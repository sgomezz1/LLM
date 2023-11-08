"""OpenLLM CLI interface.

This module also contains the SDK to call ``start`` and ``build`` from SDK

Start any LLM:

```python
openllm.start('mistral', model_id='mistralai/Mistral-7B-v0.1')
```

Build a BentoLLM

```python
bento = openllm.build('mistralai/Mistral-7B-v0.1')
```

Import any LLM into local store
```python
bentomodel = openllm.import_model('mistralai/Mistral-7B-v0.1')
```
"""
from __future__ import annotations
import enum
import functools
import importlib.util
import inspect
import itertools
import logging
import os
import platform
import random
import subprocess
import time
import traceback
import typing as t

import attr
import click
import click_option_group as cog
import fs
import fs.copy
import fs.errors
import inflection
import orjson

from bentoml_cli.utils import BentoMLCommandGroup
from bentoml_cli.utils import opt_callback
from simple_di import Provide
from simple_di import inject

import bentoml
import openllm

from bentoml._internal.configuration.containers import BentoMLContainer
from bentoml._internal.models.model import ModelStore
from openllm import bundle
from openllm.exceptions import OpenLLMException
from openllm_core._typing_compat import Concatenate
from openllm_core._typing_compat import DictStrAny
from openllm_core._typing_compat import LiteralBackend
from openllm_core._typing_compat import LiteralQuantise
from openllm_core._typing_compat import LiteralSerialisation
from openllm_core._typing_compat import LiteralString
from openllm_core._typing_compat import NotRequired
from openllm_core._typing_compat import ParamSpec
from openllm_core._typing_compat import Self
from openllm_core.config import CONFIG_MAPPING
from openllm_core.utils import DEBUG_ENV_VAR
from openllm_core.utils import OPTIONAL_DEPENDENCIES
from openllm_core.utils import QUIET_ENV_VAR
from openllm_core.utils import LazyLoader
from openllm_core.utils import analytics
from openllm_core.utils import check_bool_env
from openllm_core.utils import compose
from openllm_core.utils import configure_logging
from openllm_core.utils import first_not_none
from openllm_core.utils import get_quiet_mode
from openllm_core.utils import is_torch_available
from openllm_core.utils import resolve_user_filepath
from openllm_core.utils import set_debug_mode
from openllm_core.utils import set_quiet_mode

from . import termui
from ._factory import FC
from ._factory import LiteralOutput
from ._factory import _AnyCallable
from ._factory import backend_option
from ._factory import container_registry_option
from ._factory import machine_option
from ._factory import model_name_argument
from ._factory import model_version_option
from ._factory import parse_config_options
from ._factory import prompt_template_file_option
from ._factory import quantize_option
from ._factory import serialisation_option
from ._factory import start_decorator
from ._factory import system_message_option

if t.TYPE_CHECKING:
  import torch

  from bentoml._internal.bento import BentoStore
  from bentoml._internal.container import DefaultBuilder
  from openllm_client._schemas import StreamingResponse
  from openllm_core._configuration import LLMConfig
  from openllm_core._typing_compat import LiteralContainerRegistry
  from openllm_core._typing_compat import LiteralContainerVersionStrategy
else:
  torch = LazyLoader('torch', globals(), 'torch')

P = ParamSpec('P')
logger = logging.getLogger(__name__)
OPENLLM_FIGLET = '''\
 ██████╗ ██████╗ ███████╗███╗   ██╗██╗     ██╗     ███╗   ███╗
██╔═══██╗██╔══██╗██╔════╝████╗  ██║██║     ██║     ████╗ ████║
██║   ██║██████╔╝█████╗  ██╔██╗ ██║██║     ██║     ██╔████╔██║
██║   ██║██╔═══╝ ██╔══╝  ██║╚██╗██║██║     ██║     ██║╚██╔╝██║
╚██████╔╝██║     ███████╗██║ ╚████║███████╗███████╗██║ ╚═╝ ██║
 ╚═════╝ ╚═╝     ╚══════╝╚═╝  ╚═══╝╚══════╝╚══════╝╚═╝     ╚═╝
'''

ServeCommand = t.Literal['serve', 'serve-grpc']

@attr.define
class GlobalOptions:
  cloud_context: str | None = attr.field(default=None)

  def with_options(self, **attrs: t.Any) -> Self:
    return attr.evolve(self, **attrs)

GrpType = t.TypeVar('GrpType', bound=click.Group)

_object_setattr = object.__setattr__

_EXT_FOLDER = os.path.abspath(os.path.join(os.path.dirname(__file__), 'extension'))

class Extensions(click.MultiCommand):
  def list_commands(self, ctx: click.Context) -> list[str]:
    return sorted([filename[:-3] for filename in os.listdir(_EXT_FOLDER) if filename.endswith('.py') and not filename.startswith('__')])

  def get_command(self, ctx: click.Context, cmd_name: str) -> click.Command | None:
    try:
      mod = __import__(f'openllm.cli.extension.{cmd_name}', None, None, ['cli'])
    except ImportError:
      return None
    return mod.cli

class OpenLLMCommandGroup(BentoMLCommandGroup):
  NUMBER_OF_COMMON_PARAMS = 5  # parameters in common_params + 1 faked group option header

  @staticmethod
  def common_params(f: t.Callable[P, t.Any]) -> t.Callable[[FC], FC]:
    # The following logics is similar to one of BentoMLCommandGroup
    @cog.optgroup.group(name='Global options', help='Shared globals options for all OpenLLM CLI.')  # type: ignore[misc]
    @cog.optgroup.option('-q', '--quiet', envvar=QUIET_ENV_VAR, is_flag=True, default=False, help='Suppress all output.', show_envvar=True)
    @cog.optgroup.option('--debug', '--verbose', 'debug', envvar=DEBUG_ENV_VAR, is_flag=True, default=False, help='Print out debug logs.', show_envvar=True)
    @cog.optgroup.option('--do-not-track', is_flag=True, default=False, envvar=analytics.OPENLLM_DO_NOT_TRACK, help='Do not send usage info', show_envvar=True)
    @cog.optgroup.option('--context', 'cloud_context', envvar='BENTOCLOUD_CONTEXT', type=click.STRING, default=None, help='BentoCloud context name.', show_envvar=True)
    @click.pass_context
    @functools.wraps(f)
    def wrapper(ctx: click.Context, quiet: bool, debug: bool, cloud_context: str | None, *args: P.args, **attrs: P.kwargs) -> t.Any:
      ctx.obj = GlobalOptions(cloud_context=cloud_context)
      if quiet:
        set_quiet_mode(True)
        if debug: logger.warning("'--quiet' passed; ignoring '--verbose/--debug'")
      elif debug: set_debug_mode(True)
      configure_logging()
      return f(*args, **attrs)

    return wrapper

  @staticmethod
  def usage_tracking(func: t.Callable[P, t.Any], group: click.Group, **attrs: t.Any) -> t.Callable[Concatenate[bool, P], t.Any]:
    command_name = attrs.get('name', func.__name__)

    @functools.wraps(func)
    def wrapper(do_not_track: bool, *args: P.args, **attrs: P.kwargs) -> t.Any:
      if do_not_track:
        with analytics.set_bentoml_tracking():
          return func(*args, **attrs)
      start_time = time.time_ns()
      with analytics.set_bentoml_tracking():
        if group.name is None: raise ValueError('group.name should not be None')
        event = analytics.OpenllmCliEvent(cmd_group=group.name, cmd_name=command_name)
        try:
          return_value = func(*args, **attrs)
          duration_in_ms = (time.time_ns() - start_time) / 1e6
          event.duration_in_ms = duration_in_ms
          analytics.track(event)
          return return_value
        except Exception as e:
          duration_in_ms = (time.time_ns() - start_time) / 1e6
          event.duration_in_ms = duration_in_ms
          event.error_type = type(e).__name__
          event.return_code = 2 if isinstance(e, KeyboardInterrupt) else 1
          analytics.track(event)
          raise

    return t.cast(t.Callable[Concatenate[bool, P], t.Any], wrapper)

  def get_command(self, ctx: click.Context, cmd_name: str) -> click.Command | None:
    if cmd_name in t.cast('Extensions', extension_command).list_commands(ctx):
      return t.cast('Extensions', extension_command).get_command(ctx, cmd_name)
    cmd_name = self.resolve_alias(cmd_name)
    return super().get_command(ctx, cmd_name)

  def list_commands(self, ctx: click.Context) -> list[str]:
    return super().list_commands(ctx) + t.cast('Extensions', extension_command).list_commands(ctx)

  def command(self, *args: t.Any, **kwargs: t.Any) -> t.Callable[[t.Callable[..., t.Any]], click.Command]:  # type: ignore[override] # XXX: fix decorator on BentoMLCommandGroup
    """Override the default 'cli.command' with supports for aliases for given command, and it wraps the implementation with common parameters."""
    if 'context_settings' not in kwargs: kwargs['context_settings'] = {}
    if 'max_content_width' not in kwargs['context_settings']: kwargs['context_settings']['max_content_width'] = 120
    aliases = kwargs.pop('aliases', None)

    def decorator(f: _AnyCallable) -> click.Command:
      name = f.__name__.lower()
      if name.endswith('_command'): name = name[:-8]
      name = name.replace('_', '-')
      kwargs.setdefault('help', inspect.getdoc(f))
      kwargs.setdefault('name', name)
      wrapped = self.usage_tracking(self.common_params(f), self, **kwargs)

      # move common parameters to end of the parameters list
      _memo = getattr(wrapped, '__click_params__', None)
      if _memo is None: raise ValueError('Click command not register correctly.')
      _object_setattr(wrapped, '__click_params__', _memo[-self.NUMBER_OF_COMMON_PARAMS:] + _memo[:-self.NUMBER_OF_COMMON_PARAMS])
      # NOTE: we need to call super of super to avoid conflict with BentoMLCommandGroup command setup
      cmd = super(BentoMLCommandGroup, self).command(*args, **kwargs)(wrapped)
      # NOTE: add aliases to a given commands if it is specified.
      if aliases is not None:
        if not cmd.name: raise ValueError('name is required when aliases are available.')
        self._commands[cmd.name] = aliases
        self._aliases.update({alias: cmd.name for alias in aliases})
      return cmd

    return decorator

  def format_commands(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
    """Additional format methods that include extensions as well as the default cli command."""
    from gettext import gettext as _
    commands: list[tuple[str, click.Command]] = []
    extensions: list[tuple[str, click.Command]] = []
    _cached_extensions: list[str] = t.cast('Extensions', extension_command).list_commands(ctx)
    for subcommand in self.list_commands(ctx):
      cmd = self.get_command(ctx, subcommand)
      if cmd is None or cmd.hidden: continue
      if subcommand in _cached_extensions: extensions.append((subcommand, cmd))
      else: commands.append((subcommand, cmd))
    # allow for 3 times the default spacing
    if len(commands):
      limit = formatter.width - 6 - max(len(cmd[0]) for cmd in commands)
      rows: list[tuple[str, str]] = []
      for subcommand, cmd in commands:
        help = cmd.get_short_help_str(limit)
        rows.append((subcommand, help))
      if rows:
        with formatter.section(_('Commands')):
          formatter.write_dl(rows)
    if len(extensions):
      limit = formatter.width - 6 - max(len(cmd[0]) for cmd in extensions)
      rows = []
      for subcommand, cmd in extensions:
        help = cmd.get_short_help_str(limit)
        rows.append((inflection.dasherize(subcommand), help))
      if rows:
        with formatter.section(_('Extensions')):
          formatter.write_dl(rows)

@click.group(cls=OpenLLMCommandGroup, context_settings=termui.CONTEXT_SETTINGS, name='openllm')
@click.version_option(None, '--version', '-v', message=f'%(prog)s, %(version)s (compiled: {openllm.COMPILED})\nPython ({platform.python_implementation()}) {platform.python_version()}')
def cli() -> None:
  """\b
   ██████╗ ██████╗ ███████╗███╗   ██╗██╗     ██╗     ███╗   ███╗
  ██╔═══██╗██╔══██╗██╔════╝████╗  ██║██║     ██║     ████╗ ████║
  ██║   ██║██████╔╝█████╗  ██╔██╗ ██║██║     ██║     ██╔████╔██║
  ██║   ██║██╔═══╝ ██╔══╝  ██║╚██╗██║██║     ██║     ██║╚██╔╝██║
  ╚██████╔╝██║     ███████╗██║ ╚████║███████╗███████╗██║ ╚═╝ ██║
   ╚═════╝ ╚═╝     ╚══════╝╚═╝  ╚═══╝╚══════╝╚══════╝╚═╝     ╚═╝.

  \b
  An open platform for operating large language models in production.
  Fine-tune, serve, deploy, and monitor any LLMs with ease.
  """

@cli.command(context_settings=termui.CONTEXT_SETTINGS, name='start', aliases=['start-http'], short_help='Start a LLMServer for any supported LLM.')
@click.argument('model_id', type=click.STRING, metavar='[REMOTE_REPO/MODEL_ID | /path/to/local/model]', required=True)
@start_decorator(serve_grpc=False)
def start_command(model_id: str, server_timeout: int, model_version: str | None, system_message: str | None, prompt_template_file: t.IO[t.Any] | None,
                  workers_per_resource: t.Literal['conserved', 'round_robin'] | LiteralString, device: t.Tuple[str, ...], quantize: LiteralQuantise | None, backend: LiteralBackend | None,
                  serialisation: LiteralSerialisation | None, cors: bool, adapter_id: str | None, return_process: bool, **attrs: t.Any) -> LLMConfig | subprocess.Popen[bytes]:
  """Start any LLM as a REST server.

  \b
  ```bash
  $ openllm <start|start-http> <model_id> --<options> ...
  ```
  """
  adapter_map: dict[str, str] | None = attrs.pop('adapter_map', None)
  prompt_template = prompt_template_file.read() if prompt_template_file is not None else None

  from ..serialisation.transformers.weights import has_safetensors_weights
  serialisation = t.cast(LiteralSerialisation, first_not_none(serialisation, default='safetensors' if has_safetensors_weights(model_id) else 'legacy'))
  if serialisation == 'safetensors' and quantize is not None and check_bool_env('OPENLLM_SERIALIZATION_WARNING'):
    termui.echo(
        f"'--quantize={quantize}' might not work with 'safetensors' serialisation format. To silence this warning, set \"OPENLLM_SERIALIZATION_WARNING=False\"\nNote: You can always fallback to '--serialisation legacy' when running quantisation.",
        fg='yellow')
    termui.echo(f"Make sure to check out '{model_id}' repository to see if the weights is in '{serialisation}' format if unsure.")

  llm = openllm.LLM[t.Any, t.Any](model_id=model_id,
                                  model_version=model_version,
                                  prompt_template=prompt_template,
                                  system_message=system_message,
                                  backend=backend,
                                  adapter_map=adapter_map,
                                  quantize=quantize,
                                  serialisation=serialisation,
                                  trust_remote_code=check_bool_env('TRUST_REMOTE_CODE'))

  config, server_attrs = llm.config.model_validate_click(**attrs)
  server_timeout = first_not_none(server_timeout, default=config['timeout'])
  server_attrs.update({'working_dir': os.path.dirname(os.path.dirname(__file__)), 'timeout': server_timeout})
  # XXX: currently, theres no development args in bentoml.Server. To be fixed upstream.
  development = server_attrs.pop('development')
  server_attrs.setdefault('production', not development)
  wpr = first_not_none(workers_per_resource, default=config['workers_per_resource'])
  if isinstance(wpr, str):
    if wpr == 'round_robin': wpr = 1.0
    elif wpr == 'conserved':
      if device and openllm.utils.device_count() == 0:
        termui.echo('--device will have no effect as there is no GPUs available', fg='yellow')
        wpr = 1.0
      else:
        available_gpu = len(device) if device else openllm.utils.device_count()
        wpr = 1.0 if available_gpu == 0 else float(1 / available_gpu)
    else:
      wpr = float(wpr)
  elif isinstance(wpr, int):
    wpr = float(wpr)

  requirements = llm.config['requirements']
  if requirements is not None and len(requirements) > 0:
    missing_requirements = [i for i in requirements if importlib.util.find_spec(inflection.underscore(i)) is None]
    if len(missing_requirements) > 0:
      termui.echo(f'Make sure to have the following dependencies available: {missing_requirements}', fg='yellow')

  start_env = parse_config_options(config, server_timeout, wpr, device, cors, os.environ.copy())
  start_env.update({
      'OPENLLM_MODEL_ID': model_id,
      'BENTOML_DEBUG': str(openllm.utils.get_debug_mode()),
      'BENTOML_HOME': os.environ.get('BENTOML_HOME', BentoMLContainer.bentoml_home.get()),
      'OPENLLM_ADAPTER_MAP': orjson.dumps(adapter_map).decode(),
      'OPENLLM_SERIALIZATION': serialisation,
      'OPENLLM_BACKEND': llm.__llm_backend__,
      'OPENLLM_CONFIG': llm.config.model_dump_json().decode(),
  })
  if llm._quantise: start_env['OPENLLM_QUANTIZE'] = str(llm._quantise)
  if system_message: start_env['OPENLLM_SYSTEM_MESSAGE'] = system_message
  if prompt_template: start_env['OPENLLM_PROMPT_TEMPLATE'] = prompt_template

  server = bentoml.HTTPServer('_service:svc', **server_attrs)
  openllm.utils.analytics.track_start_init(llm.config)

  def next_step(adapter_map: DictStrAny | None) -> None:
    cmd_name = f'openllm build {model_id}'
    if llm._quantise: cmd_name += f' --quantize {llm._quantise}'
    cmd_name += f' --serialization {serialisation}'
    if adapter_map is not None:
      cmd_name += ' ' + ' '.join([f'--adapter-id {s}' for s in [f'{p}:{name}' if name not in (None, 'default') else p for p, name in adapter_map.items()]])
    if not openllm.utils.get_quiet_mode():
      termui.echo(f"\n🚀 Next step: run '{cmd_name}' to create a BentoLLM for '{model_id}'", fg='blue')

  if return_process:
    server.start(env=start_env, text=True)
    if server.process is None: raise click.ClickException('Failed to start the server.')
    return server.process
  else:
    try:
      server.start(env=start_env, text=True, blocking=True)
    except Exception as err:
      termui.echo(f'Error caught while running LLM Server:\n{err}', fg='red')
      raise
    else:
      next_step(adapter_map)

  # NOTE: Return the configuration for telemetry purposes.
  return config

@cli.command(context_settings=termui.CONTEXT_SETTINGS, name='start-grpc', short_help='Start a gRPC LLMServer for any supported LLM.')
@click.argument('model_id', type=click.STRING, metavar='[REMOTE_REPO/MODEL_ID | /path/to/local/model]', required=True)
@start_decorator(serve_grpc=True)
def start_grpc_command(model_id: str, server_timeout: int, model_version: str | None, system_message: str | None, prompt_template_file: t.IO[t.Any] | None,
                       workers_per_resource: t.Literal['conserved', 'round_robin'] | LiteralString, device: t.Tuple[str, ...], quantize: LiteralQuantise | None, backend: LiteralBackend | None,
                       serialisation: LiteralSerialisation | None, cors: bool, adapter_id: str | None, return_process: bool, **attrs: t.Any) -> LLMConfig | subprocess.Popen[bytes]:
  """Start any LLM as a gRPC server.

  \b
  ```bash
  $ openllm start-grpc <model_id> --<options> ...
  ```
  """
  adapter_map: dict[str, str] | None = attrs.pop('adapter_map', None)
  prompt_template = prompt_template_file.read() if prompt_template_file is not None else None

  from ..serialisation.transformers.weights import has_safetensors_weights
  serialisation = t.cast(LiteralSerialisation, first_not_none(serialisation, default='safetensors' if has_safetensors_weights(model_id) else 'legacy'))
  if serialisation == 'safetensors' and quantize is not None and check_bool_env('OPENLLM_SERIALIZATION_WARNING'):
    termui.echo(
        f"'--quantize={quantize}' might not work with 'safetensors' serialisation format. To silence this warning, set \"OPENLLM_SERIALIZATION_WARNING=False\"\nNote: You can always fallback to '--serialisation legacy' when running quantisation.",
        fg='yellow')
    termui.echo(f"Make sure to check out '{model_id}' repository to see if the weights is in '{serialisation}' format if unsure.")

  llm = openllm.LLM[t.Any, t.Any](model_id=model_id,
                                  model_version=model_version,
                                  prompt_template=prompt_template,
                                  system_message=system_message,
                                  backend=backend,
                                  adapter_map=adapter_map,
                                  quantize=quantize,
                                  serialisation=serialisation,
                                  trust_remote_code=check_bool_env('TRUST_REMOTE_CODE'))

  config, server_attrs = llm.config.model_validate_click(**attrs)
  server_timeout = first_not_none(server_timeout, default=config['timeout'])
  server_attrs.update({'working_dir': os.path.dirname(os.path.dirname(__file__)), 'timeout': server_timeout})
  server_attrs['grpc_protocol_version'] = 'v1'
  # XXX: currently, theres no development args in bentoml.Server. To be fixed upstream.
  development = server_attrs.pop('development')
  server_attrs.setdefault('production', not development)
  wpr = first_not_none(workers_per_resource, default=config['workers_per_resource'])
  if isinstance(wpr, str):
    if wpr == 'round_robin': wpr = 1.0
    elif wpr == 'conserved':
      if device and openllm.utils.device_count() == 0:
        termui.echo('--device will have no effect as there is no GPUs available', fg='yellow')
        wpr = 1.0
      else:
        available_gpu = len(device) if device else openllm.utils.device_count()
        wpr = 1.0 if available_gpu == 0 else float(1 / available_gpu)
    else:
      wpr = float(wpr)
  elif isinstance(wpr, int):
    wpr = float(wpr)

  requirements = llm.config['requirements']
  if requirements is not None and len(requirements) > 0:
    missing_requirements = [i for i in requirements if importlib.util.find_spec(inflection.underscore(i)) is None]
    if len(missing_requirements) > 0:
      termui.echo(f'Make sure to have the following dependencies available: {missing_requirements}', fg='yellow')

  start_env = parse_config_options(config, server_timeout, wpr, device, cors, os.environ.copy())
  start_env.update({
      'OPENLLM_MODEL_ID': model_id,
      'BENTOML_DEBUG': str(openllm.utils.get_debug_mode()),
      'BENTOML_HOME': os.environ.get('BENTOML_HOME', BentoMLContainer.bentoml_home.get()),
      'OPENLLM_ADAPTER_MAP': orjson.dumps(adapter_map).decode(),
      'OPENLLM_SERIALIZATION': serialisation,
      'OPENLLM_BACKEND': llm.__llm_backend__,
      'OPENLLM_CONFIG': llm.config.model_dump_json().decode(),
  })
  if llm._quantise: start_env['OPENLLM_QUANTIZE'] = str(llm._quantise)
  if system_message: start_env['OPENLLM_SYSTEM_MESSAGE'] = system_message
  if prompt_template: start_env['OPENLLM_PROMPT_TEMPLATE'] = prompt_template

  server = bentoml.GrpcServer('_service:svc', **server_attrs)
  openllm.utils.analytics.track_start_init(llm.config)

  def next_step(adapter_map: DictStrAny | None) -> None:
    cmd_name = f'openllm build {model_id}'
    if llm._quantise: cmd_name += f' --quantize {llm._quantise}'
    cmd_name += f' --serialization {serialisation}'
    if adapter_map is not None:
      cmd_name += ' ' + ' '.join([f'--adapter-id {s}' for s in [f'{p}:{name}' if name not in (None, 'default') else p for p, name in adapter_map.items()]])
    if not openllm.utils.get_quiet_mode():
      termui.echo(f"\n🚀 Next step: run '{cmd_name}' to create a BentoLLM for '{model_id}'", fg='blue')

  if return_process:
    server.start(env=start_env, text=True)
    if server.process is None: raise click.ClickException('Failed to start the server.')
    return server.process
  else:
    try:
      server.start(env=start_env, text=True, blocking=True)
    except Exception as err:
      termui.echo(f'Error caught while running LLM Server:\n{err}', fg='red')
      raise
    else:
      next_step(adapter_map)

  # NOTE: Return the configuration for telemetry purposes.
  return config

class ItemState(enum.Enum):
  NOT_FOUND = 'NOT_FOUND'
  EXISTS = 'EXISTS'
  OVERWRITE = 'OVERWRITE'

class ImportModelOutput(t.TypedDict):
  previous_state: ItemState
  backend: LiteralBackend
  tag: str

@cli.command(name='import', aliases=['download'])
@click.argument('model_id', type=click.STRING, metavar='[REMOTE_REPO/MODEL_ID | /path/to/local/model]', required=True)
@model_version_option
@backend_option
@quantize_option
@serialisation_option
def import_command(model_id: str, model_version: str | None, backend: LiteralBackend | None, quantize: LiteralQuantise | None, serialisation: LiteralSerialisation | None) -> dict[str, t.Any]:
  """Setup LLM interactively.

  \b
  This `model_id` can be either pretrained model id that you can get from HuggingFace Hub, or
  a custom model path from your custom pretrained model. Note that the custom model path should
  contain all files required to construct `transformers.PretrainedConfig`, `transformers.PreTrainedModel`
  and `transformers.PreTrainedTokenizer` objects.

  \b
  Note that if `--serialisation` is not defined, then we will try to infer serialisation from HuggingFace Hub.
  If the model id contains safetensors weights, then we will use `safetensors` serialisation. Otherwise, we will
  fallback to `legacy` '.bin' (otherwise known as pickle) serialisation.

  \b
  ``--model-version`` is an optional option to save the model. Note that
  this is recommended when the model_id is a custom path. Usually, if you are only using pretrained
  model from HuggingFace Hub, you don't need to specify this. If this is not specified, we will calculate
  the hash from the last modified time from this custom path

  \b
  ```bash
  $ openllm import mistralai/Mistral-7B-v0.1
  ```

  \b
  > If ``quantize`` is passed, the model weights will be saved as quantized weights. You should
  > only use this option if you want the weight to be quantized by default. Note that OpenLLM also
  > support on-demand quantisation during initial startup.
  """
  from ..serialisation.transformers.weights import has_safetensors_weights
  llm = openllm.LLM[t.Any, t.Any](model_id=model_id,
                                  model_version=model_version,
                                  quantize=quantize,
                                  serialisation=t.cast(LiteralSerialisation, first_not_none(serialisation, default='safetensors' if has_safetensors_weights(model_id) else 'legacy')))

  previous_state = ItemState.NOT_FOUND
  try:
    model = bentoml.models.get(llm.tag)
    previous_state = ItemState.EXISTS
  except bentoml.exceptions.NotFound:
    model = openllm.serialisation.import_model(llm, trust_remote_code=llm.trust_remote_code)
    if llm.__llm_backend__ == 'pt' and is_torch_available() and torch.cuda.is_available(): torch.cuda.empty_cache()
  response = ImportModelOutput(previous_state=previous_state, backend=llm.__llm_backend__, tag=str(model.tag))
  termui.echo(orjson.dumps(response).decode())
  return response

class BuildBentoOutput(t.TypedDict):
  previous_state: ItemState
  tag: str

@cli.command(context_settings={'token_normalize_func': inflection.underscore})
@click.argument('model_id', type=click.STRING, metavar='[REMOTE_REPO/MODEL_ID | /path/to/local/model]', required=True)
@backend_option
@system_message_option
@prompt_template_file_option
@click.option('--bento-version', type=str, default=None, help='Optional bento version for this BentoLLM. Default is the the model revision.')
@click.option('--overwrite', is_flag=True, help='Overwrite existing Bento for given LLM if it already exists.')
@cog.optgroup.group(cls=cog.MutuallyExclusiveOptionGroup, name='Optimisation options')  # type: ignore[misc]
@quantize_option(factory=cog.optgroup, build=True)
@click.option('--enable-features',
              multiple=True,
              nargs=1,
              metavar='FEATURE[,FEATURE]',
              help='Enable additional features for building this LLM Bento. Available: {}'.format(', '.join(OPTIONAL_DEPENDENCIES)))
@click.option('--adapter-id',
              default=None,
              multiple=True,
              metavar='[PATH | [remote/][adapter_name:]adapter_id][, ...]',
              help="Optional adapters id to be included within the Bento. Note that if you are using relative path, '--build-ctx' must be passed.")
@click.option('--build-ctx', help='Build context. This is required if --adapter-id uses relative path', default=None)
@model_version_option
@click.option('--dockerfile-template', default=None, type=click.File(), help='Optional custom dockerfile template to be used with this BentoLLM.')
@serialisation_option
@container_registry_option
@click.option('--container-version-strategy',
              type=click.Choice(['release', 'latest', 'nightly']),
              default='release',
              help="Default container version strategy for the image from '--container-registry'")
@cog.optgroup.group(cls=cog.MutuallyExclusiveOptionGroup, name='Utilities options')  # type: ignore[misc]
@cog.optgroup.option('--containerize',
                     default=False,
                     is_flag=True,
                     type=click.BOOL,
                     help="Whether to containerize the Bento after building. '--containerize' is the shortcut of 'openllm build && bentoml containerize'.")
@cog.optgroup.option('--push', default=False, is_flag=True, type=click.BOOL, help="Whether to push the result bento to BentoCloud. Make sure to login with 'bentoml cloud login' first.")
@click.option('--force-push', default=False, is_flag=True, type=click.BOOL, help='Whether to force push.')
@machine_option
@click.pass_context
def build_command(ctx: click.Context, /, model_id: str, bento_version: str | None, overwrite: bool, quantize: LiteralQuantise | None, machine: bool, enable_features: tuple[str, ...] | None,
                  adapter_id: tuple[str, ...], build_ctx: str | None, backend: LiteralBackend | None, system_message: str | None, prompt_template_file: t.IO[t.Any] | None,
                  model_version: str | None, dockerfile_template: t.TextIO | None, containerize: bool, push: bool, serialisation: LiteralSerialisation | None,
                  container_registry: LiteralContainerRegistry, container_version_strategy: LiteralContainerVersionStrategy, force_push: bool, **_: t.Any) -> BuildBentoOutput:
  """Package a given models into a BentoLLM.

  \b
  ```bash
  $ openllm build google/flan-t5-large
  ```

  \b
  > [!NOTE]
  > To run a container built from this Bento with GPU support, make sure
  > to have https://github.com/NVIDIA/nvidia-container-toolkit install locally.

  \b
  > [!IMPORTANT]
  > To build the bento with compiled OpenLLM, make sure to prepend HATCH_BUILD_HOOKS_ENABLE=1. Make sure that the deployment
  > target also use the same Python version and architecture as build machine.
  """
  from .._llm import normalise_model_name
  from ..serialisation.transformers.weights import has_safetensors_weights
  if enable_features: enable_features = tuple(itertools.chain.from_iterable((s.split(',') for s in enable_features)))

  previous_state = ItemState.NOT_FOUND

  prompt_template = prompt_template_file.read() if prompt_template_file is not None else None
  llm = openllm.LLM[t.Any, t.Any](model_id=model_id,
                                  model_version=model_version,
                                  prompt_template=prompt_template,
                                  system_message=system_message,
                                  backend=backend,
                                  quantize=quantize,
                                  serialisation=t.cast(LiteralSerialisation, first_not_none(serialisation, default='safetensors' if has_safetensors_weights(model_id) else 'legacy')))

  os.environ.update({'OPENLLM_BACKEND': llm.__llm_backend__, 'OPENLLM_SERIALIZATION': llm._serialisation, 'OPENLLM_MODEL_ID': llm.model_id})
  if llm._quantise: os.environ['OPENLLM_QUANTIZE'] = str(llm._quantise)
  if system_message: os.environ['OPENLLM_SYSTEM_MESSAGE'] = system_message
  if prompt_template: os.environ['OPENLLM_PROMPT_TEMPLATE'] = prompt_template

  # NOTE: We set this environment variable so that our service.py logic won't raise RuntimeError
  # during build. This is a current limitation of bentoml build where we actually import the service.py into sys.path
  try:
    assert llm.bentomodel  # HACK: call it here to patch correct tag with revision and everything
    # FIX: This is a patch for _service_vars injection
    if 'OPENLLM_ADAPTER_MAP' not in os.environ: os.environ['OPENLLM_ADAPTER_MAP'] = orjson.dumps(None).decode()

    labels = dict(llm.identifying_params)
    labels.update({'_type': llm.llm_type, '_framework': llm.__llm_backend__})

    with fs.open_fs(f'temp://llm_{normalise_model_name(model_id)}') as llm_fs:
      dockerfile_template_path = None
      if dockerfile_template:
        with dockerfile_template:
          llm_fs.writetext('Dockerfile.template', dockerfile_template.read())
        dockerfile_template_path = llm_fs.getsyspath('/Dockerfile.template')

      adapter_map: dict[str, str] | None = None
      if adapter_id and not build_ctx: ctx.fail("'build_ctx' is required when '--adapter-id' is passsed.")
      if adapter_id:
        adapter_map = {}
        for v in adapter_id:
          _adapter_id, *adapter_name = v.rsplit(':', maxsplit=1)
          name = adapter_name[0] if len(adapter_name) > 0 else 'default'
          try:
            resolve_user_filepath(_adapter_id, build_ctx)
            src_folder_name = os.path.basename(_adapter_id)
            src_fs = fs.open_fs(build_ctx)
            llm_fs.makedir(src_folder_name, recreate=True)
            fs.copy.copy_dir(src_fs, _adapter_id, llm_fs, src_folder_name)
            adapter_map[src_folder_name] = name
          # this is the remote adapter, then just added back
          # note that there is a drawback here. If the path of the local adapter
          # path have the same name as the remote, then we currently don't support
          # that edge case.
          except FileNotFoundError:
            adapter_map[_adapter_id] = name
        os.environ['OPENLLM_ADAPTER_MAP'] = orjson.dumps(adapter_map).decode()

      _bento_version = first_not_none(bento_version, default=llm.bentomodel.tag.version)
      bento_tag = bentoml.Tag.from_taglike(f'{llm.llm_type}-service:{_bento_version}'.lower().strip())
      try:
        bento = bentoml.get(bento_tag)
        if overwrite:
          bentoml.delete(bento_tag)
          previous_state = ItemState.OVERWRITE
          raise bentoml.exceptions.NotFound(f'Rebuilding existing Bento {bento_tag}') from None
        previous_state = ItemState.EXISTS
      except bentoml.exceptions.NotFound:
        bento = bundle.create_bento(bento_tag,
                                    llm_fs,
                                    llm,
                                    adapter_map=adapter_map,
                                    quantize=quantize,
                                    extra_dependencies=enable_features,
                                    dockerfile_template=dockerfile_template_path,
                                    container_registry=container_registry,
                                    container_version_strategy=container_version_strategy)
  except Exception as err:
    traceback.print_exc()
    raise click.ClickException('Exception caught while building BentoLLM:\n' + str(err)) from err

  response = BuildBentoOutput(previous_state=previous_state, tag=str(bento_tag))

  if machine: termui.echo(f'__object__:{orjson.dumps(response).decode()}\n\n', fg='white')
  elif not get_quiet_mode() and (not push or not containerize):
    termui.echo('\n' + OPENLLM_FIGLET, fg='white')
    if not previous_state != ItemState.EXISTS: termui.echo(f'Successfully built {bento}.', fg='green')
    elif not overwrite: termui.echo(f"Bento for '{model_id}' already exists [{bento}]. To overwrite it pass '--overwrite'.", fg='yellow')
    termui.echo('📖 Next steps:\n\n' + f"* Push to BentoCloud with 'bentoml push':\n\t$ bentoml push {bento_tag}\n\n" +
                f"* Containerize your Bento with 'bentoml containerize':\n\t$ bentoml containerize {bento_tag} --opt progress=plain\n\n" +
                "\tTip: To enable additional BentoML features for 'containerize', use '--enable-features=FEATURE[,FEATURE]' [see 'bentoml containerize -h' for more advanced usage]\n",
                fg='blue',
                )

  if push: BentoMLContainer.bentocloud_client.get().push_bento(bento, context=t.cast(GlobalOptions, ctx.obj).cloud_context, force=force_push)
  elif containerize:
    container_backend = t.cast('DefaultBuilder', os.environ.get('BENTOML_CONTAINERIZE_BACKEND', 'docker'))
    try:
      bentoml.container.health(container_backend)
    except subprocess.CalledProcessError:
      raise OpenLLMException(f'Failed to use backend {backend}') from None
    try:
      bentoml.container.build(bento.tag, backend=container_backend, features=('grpc', 'io'))
    except Exception as err:
      raise OpenLLMException(f"Exception caught while containerizing '{bento.tag!s}':\n{err}") from err

  return response

class ModelItem(t.TypedDict):
  architecture: str
  example_id: str
  supported_backends: t.Tuple[LiteralBackend, ...]
  installation: str
  items: NotRequired[t.List[str]]

@cli.command()
@click.option('--show-available', is_flag=True, default=False, help="Show available models in local store (mutually exclusive with '-o porcelain').")
def models_command(show_available: bool) -> t.Dict[str, t.Any]:
  """List all supported models.

  \b
  ```bash
  openllm models --show-available
  ```
  """
  result: dict[t.LiteralString, ModelItem] = {
      m: ModelItem(architecture=config.__openllm_architecture__,
                   example_id=random.choice(config.__openllm_model_ids__),
                   supported_backends=config.__openllm_backend__,
                   installation='pip install ' + (f'"openllm[{m}]"' if m in OPTIONAL_DEPENDENCIES or config.__openllm_requirements__ else 'openllm'),
                   items=[] if not show_available else [
                       str(md.tag)
                       for md in bentoml.models.list()
                       if 'framework' in md.info.labels and md.info.labels['framework'] == 'openllm' and 'model_name' in md.info.labels and md.info.labels['model_name'] == m
                   ]) for m, config in CONFIG_MAPPING.items()
  }
  termui.echo(orjson.dumps(result, option=orjson.OPT_INDENT_2).decode(), fg='white')
  return result

@cli.command()
@model_name_argument(required=False)
@click.option('-y', '--yes', '--assume-yes', is_flag=True, help='Skip confirmation when deleting a specific model')
@click.option('--include-bentos/--no-include-bentos', is_flag=True, default=False, help='Whether to also include pruning bentos.')
@inject
def prune_command(model_name: str | None,
                  yes: bool,
                  include_bentos: bool,
                  model_store: ModelStore = Provide[BentoMLContainer.model_store],
                  bento_store: BentoStore = Provide[BentoMLContainer.bento_store]) -> None:
  """Remove all saved models, (and optionally bentos) built with OpenLLM locally.

  \b
  If a model type is passed, then only prune models for that given model type.
  """
  available: list[tuple[bentoml.Model | bentoml.Bento,
                        ModelStore | BentoStore]] = [(m, model_store) for m in bentoml.models.list() if 'framework' in m.info.labels and m.info.labels['framework'] == 'openllm']
  if model_name is not None: available = [(m, store) for m, store in available if 'model_name' in m.info.labels and m.info.labels['model_name'] == inflection.underscore(model_name)]
  if include_bentos:
    if model_name is not None:
      available += [(b, bento_store) for b in bentoml.bentos.list() if 'start_name' in b.info.labels and b.info.labels['start_name'] == inflection.underscore(model_name)]
    else:
      available += [(b, bento_store) for b in bentoml.bentos.list() if '_type' in b.info.labels and '_framework' in b.info.labels]

  for store_item, store in available:
    if yes: delete_confirmed = True
    else: delete_confirmed = click.confirm(f"delete {'model' if isinstance(store, ModelStore) else 'bento'} {store_item.tag}?")
    if delete_confirmed:
      store.delete(store_item.tag)
      termui.echo(f"{store_item} deleted from {'model' if isinstance(store, ModelStore) else 'bento'} store.", fg='yellow')

def parsing_instruction_callback(ctx: click.Context, param: click.Parameter, value: list[str] | str | None) -> tuple[str, bool | str] | list[str] | str | None:
  if value is None:
    return value

  if isinstance(value, list):
    # we only parse --text foo bar -> --text foo and omit bar
    value = value[-1]

  key, *values = value.split('=')
  if not key.startswith('--'):
    raise click.BadParameter(f'Invalid option format: {value}')
  key = key[2:]
  if len(values) == 0:
    return key, True
  elif len(values) == 1:
    return key, values[0]
  else:
    raise click.BadParameter(f'Invalid option format: {value}')

def shared_client_options(f: _AnyCallable | None = None) -> t.Callable[[FC], FC]:
  options = [
      click.option('--endpoint',
                   type=click.STRING,
                   help='OpenLLM Server endpoint, i.e: http://localhost:3000',
                   envvar='OPENLLM_ENDPOINT',
                   show_envvar=True,
                   show_default=True,
                   default='http://localhost:3000'),
      click.option('--timeout', type=click.INT, default=30, help='Default server timeout', show_default=True),
  ]
  return compose(*options)(f) if f is not None else compose(*options)

@cli.command(hidden=True)
@click.argument('task', type=click.STRING, metavar='TASK')
@shared_client_options
@click.option('--agent', type=click.Choice(['hf']), default='hf', help='Whether to interact with Agents from given Server endpoint.', show_default=True)
@click.option('--remote', is_flag=True, default=False, help='Whether or not to use remote tools (inference endpoints) instead of local ones.', show_default=True)
@click.option('--opt',
              help="Define prompt options. (format: ``--opt text='I love this' --opt audio:./path/to/audio  --opt image:/path/to/file``)",
              required=False,
              multiple=True,
              callback=opt_callback,
              metavar='ARG=VALUE[,ARG=VALUE]')
def instruct_command(endpoint: str, timeout: int, agent: LiteralString, output: LiteralOutput, remote: bool, task: str, _memoized: DictStrAny, **attrs: t.Any) -> str:
  '''Instruct agents interactively for given tasks, from a terminal.

  \b
  ```bash
  $ openllm instruct --endpoint http://12.323.2.1:3000 \\
        "Is the following `text` (in Spanish) positive or negative?" \\
        --text "¡Este es un API muy agradable!"
  ```
  '''
  raise click.ClickException("'instruct' is currently disabled")
  # client = openllm.client.HTTPClient(endpoint, timeout=timeout)
  #
  # try:
  #   client.call('metadata')
  # except http.client.BadStatusLine:
  #   raise click.ClickException(f'{endpoint} is neither a HTTP server nor reachable.') from None
  # if agent == 'hf':
  #   _memoized = {k: v[0] for k, v in _memoized.items() if v}
  #   client._hf_agent.set_stream(logger.info)
  #   if output != 'porcelain': termui.echo(f"Sending the following prompt ('{task}') with the following vars: {_memoized}", fg='magenta')
  #   result = client.ask_agent(task, agent_type=agent, return_code=False, remote=remote, **_memoized)
  #   if output == 'json': termui.echo(orjson.dumps(result, option=orjson.OPT_INDENT_2).decode(), fg='white')
  #   else: termui.echo(result, fg='white')
  #   return result
  # else:
  #   raise click.BadOptionUsage('agent', f'Unknown agent type {agent}')

@cli.command()
@shared_client_options
@click.option('--server-type', type=click.Choice(['grpc', 'http']), help='Server type', default='http', show_default=True, hidden=True)
@click.option('--stream/--no-stream', type=click.BOOL, is_flag=True, default=True, help='Whether to stream the response.')
@click.argument('prompt', type=click.STRING)
@click.option('--sampling-params',
              help='Define query options. (format: ``--opt temperature=0.8 --opt=top_k:12)',
              required=False,
              multiple=True,
              callback=opt_callback,
              metavar='ARG=VALUE[,ARG=VALUE]')
@click.pass_context
def query_command(ctx: click.Context, /, prompt: str, endpoint: str, timeout: int, stream: bool, server_type: t.Literal['http', 'grpc'], _memoized: DictStrAny, **_: t.Any) -> None:
  '''Query a LLM interactively, from a terminal.

  \b
  ```bash
  $ openllm query --endpoint http://12.323.2.1:3000 "What is the meaning of life?"
  ```
  '''
  if server_type == 'grpc': raise click.ClickException("'grpc' is currently disabled.")
  _memoized = {k: orjson.loads(v[0]) for k, v in _memoized.items() if v}
  # TODO: grpc support
  client = openllm.client.HTTPClient(address=endpoint, timeout=timeout)
  input_fg, generated_fg = 'magenta', 'cyan'

  if stream:
    stream_res: t.Iterator[StreamingResponse] = client.generate_stream(prompt, **_memoized)
    termui.echo(prompt, fg=input_fg, nl=False)
    for it in stream_res:
      termui.echo(it.text, fg=generated_fg, nl=False)
  else:
    termui.echo(prompt, fg=input_fg, nl=False)
    termui.echo(client.generate(prompt, **_memoized).outputs[0].text, fg=generated_fg, nl=False)
  ctx.exit(0)

@cli.group(cls=Extensions, hidden=True, name='extension')
def extension_command() -> None:
  """Extension for OpenLLM CLI."""

if __name__ == '__main__': cli()
