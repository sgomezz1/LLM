# mypy: disable-error-code="name-defined,attr-defined"
from __future__ import annotations
import asyncio
import logging
import os
import types
import typing as t
import warnings

import attr
import inflection
import orjson

from huggingface_hub import hf_hub_download

import bentoml
import openllm
import openllm_core

from bentoml._internal.models.model import ModelSignature
from bentoml._internal.runner.runner_handle import DummyRunnerHandle
from openllm_core._schemas import GenerationOutput
from openllm_core._strategies import CascadingResourceStrategy
from openllm_core._typing_compat import AdaptersMapping
from openllm_core._typing_compat import AdaptersTuple
from openllm_core._typing_compat import AdapterType
from openllm_core._typing_compat import DictStrAny
from openllm_core._typing_compat import InferenceReturnType
from openllm_core._typing_compat import LiteralBackend
from openllm_core._typing_compat import LiteralQuantise
from openllm_core._typing_compat import LiteralSerialisation
from openllm_core._typing_compat import LLMRunnable
from openllm_core._typing_compat import LLMRunner
from openllm_core._typing_compat import M
from openllm_core._typing_compat import ModelSignatureDict
from openllm_core._typing_compat import ParamSpec
from openllm_core._typing_compat import T
from openllm_core._typing_compat import TupleAny
from openllm_core.prompts import PromptTemplate
from openllm_core.utils import LazyLoader
from openllm_core.utils import ReprMixin
from openllm_core.utils import apply
from openllm_core.utils import codegen
from openllm_core.utils import converter
from openllm_core.utils import first_not_none
from openllm_core.utils import flatten_attrs
from openllm_core.utils import generate_hash_from_file
from openllm_core.utils import is_peft_available
from openllm_core.utils import is_torch_available
from openllm_core.utils import is_vllm_available
from openllm_core.utils import resolve_filepath
from openllm_core.utils import validate_is_path

from ._quantisation import infer_quantisation_config
from .exceptions import ForbiddenAttributeError
from .exceptions import OpenLLMException
from .serialisation.constants import PEFT_CONFIG_NAME

if t.TYPE_CHECKING:
  import peft
  import torch
  import transformers

  from openllm_core._configuration import LLMConfig
  from openllm_core.utils.representation import ReprArgs

else:
  transformers = LazyLoader('transformers', globals(), 'transformers')
  torch = LazyLoader('torch', globals(), 'torch')
  peft = LazyLoader('peft', globals(), 'peft')

ResolvedAdaptersMapping = t.Dict[AdapterType, t.Dict[str, t.Tuple['peft.PeftConfig', str]]]

P = ParamSpec('P')

logger = logging.getLogger(__name__)

def normalise_model_name(name: str) -> str:
  if validate_is_path(name): return os.path.basename(resolve_filepath(name))
  name = name.replace('/', '--')
  return inflection.dasherize(name)

def resolve_peft_config_type(adapter_map: dict[str, str | None]) -> AdaptersMapping:
  '''Resolve the type of the PeftConfig given the adapter_map.

  This is similar to how PeftConfig resolve its config type.

  Args:
  adapter_map: The given mapping from either SDK or CLI. See CLI docs for more information.
  '''
  resolved: AdaptersMapping = {}
  _has_set_default = False
  for path_or_adapter_id, name in adapter_map.items():
    resolve_name = name
    if resolve_name is None:
      if _has_set_default: raise ValueError('Only one adapter can be set as default.')
      resolve_name = 'default'
      _has_set_default = True
    if os.path.isfile(os.path.join(path_or_adapter_id, PEFT_CONFIG_NAME)):
      config_file = os.path.join(path_or_adapter_id, PEFT_CONFIG_NAME)
    else:
      try:
        config_file = hf_hub_download(path_or_adapter_id, PEFT_CONFIG_NAME)
      except Exception as err:
        raise ValueError(f"Can't find '{PEFT_CONFIG_NAME}' at '{path_or_adapter_id}'") from err
    with open(config_file, 'r') as file:
      resolved_config = orjson.loads(file.read())
    # all peft_type should be available in PEFT_CONFIG_NAME
    _peft_type: AdapterType = resolved_config['peft_type'].lower()
    if _peft_type not in resolved: resolved[_peft_type] = ()
    resolved[_peft_type] += (_AdaptersTuple((path_or_adapter_id, resolve_name, resolved_config)),)
  return resolved

_reserved_namespace = {'model', 'tokenizer', 'runner', 'import_kwargs'}
_AdaptersTuple: type[AdaptersTuple] = codegen.make_attr_tuple_class('AdaptersTuple', ['adapter_id', 'name', 'config'])

@attr.define(slots=True, repr=False, init=False)
class LLM(t.Generic[M, T]):
  _model_id: str
  _revision: str | None
  quantization_config: transformers.BitsAndBytesConfig | transformers.GPTQConfig | transformers.AwqConfig | None
  _quantise: LiteralQuantise | None
  _model_decls: TupleAny
  _model_attrs: DictStrAny
  _tokenizer_attrs: DictStrAny
  _tag: bentoml.Tag
  _adapter_map: AdaptersMapping | None
  _serialisation: LiteralSerialisation
  _local: bool
  _prompt_template: PromptTemplate | None
  _system_message: str | None

  __llm_config__: LLMConfig | None = None
  __llm_backend__: LiteralBackend = None  # type: ignore
  __llm_runner__: t.Optional[LLMRunner[M, T]] = None
  __llm_model__: t.Optional[M] = None
  __llm_tokenizer__: t.Optional[T] = None
  __llm_adapter_map__: t.Optional[ResolvedAdaptersMapping] = None

  device: 'torch.device' | None = None

  def __attrs_post_init__(self) -> None:
    if self.__llm_backend__ == 'pt': self.device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')

  def __init__(self,
               model_id: str,
               model_version: str | None = None,
               model_tag: str | bentoml.Tag | None = None,
               prompt_template: PromptTemplate | str | None = None,
               system_message: str | None = None,
               llm_config: LLMConfig | None = None,
               backend: LiteralBackend | None = None,
               *args: t.Any,
               quantize: LiteralQuantise | None = None,
               quantization_config: transformers.BitsAndBytesConfig | transformers.GPTQConfig | transformers.AwqConfig | None = None,
               adapter_map: dict[str, str | None] | None = None,
               serialisation: LiteralSerialisation = 'safetensors',
               **attrs: t.Any):
    # low_cpu_mem_usage is only available for model
    # this is helpful on system with low memory to avoid OOM
    low_cpu_mem_usage = attrs.pop('low_cpu_mem_usage', True)
    _local = False
    if validate_is_path(model_id): model_id, _local = resolve_filepath(model_id), True
    backend = openllm.utils.first_not_none(os.getenv('OPENLLM_BACKEND'), default='vllm' if openllm.utils.is_vllm_available() else 'pt')

    quantize = first_not_none(quantize, t.cast(t.Optional[LiteralQuantise], os.getenv('OPENLLM_QUANTIZE')), default=None)
    # quantization setup
    if quantization_config and quantize:
      logger.warning("Both 'quantization_config' and 'quantize' are specified. 'quantize' will be ignored.")
    elif quantization_config is None and quantize is not None:
      # in case users input `tokenizer` to __init__, default to the _model_id
      if quantize == 'gptq': attrs.setdefault('tokenizer', model_id)
      # TODO: support AWQConfig
      quantization_config, attrs = infer_quantisation_config(self, quantize, **attrs)
    attrs.update({'low_cpu_mem_usage': low_cpu_mem_usage, 'quantization_config': quantization_config})

    # parsing tokenizer and model kwargs, as the hierarchy is param pass > default
    model_attrs, tokenizer_attrs = flatten_attrs(**attrs)

    if adapter_map is not None and not is_peft_available(): raise RuntimeError("LoRA adapter requires 'peft' to be installed. Make sure to do 'pip install \"openllm[fine-tune]\"'")
    if isinstance(prompt_template, str): prompt_template = PromptTemplate(prompt_template)
    if model_tag is None:
      model_tag, model_version = self._make_tag_components(model_id, model_version, backend=backend)
      if model_version: model_tag = f'{model_tag}:{model_version}'

    self.__attrs_init__(model_id=model_id,
                        revision=model_version,
                        tag=bentoml.Tag.from_taglike(t.cast(t.Union[str, bentoml.Tag], model_tag)),
                        quantization_config=quantization_config,
                        quantise=quantize,
                        model_decls=args,
                        model_attrs=dict(**self.import_kwargs[0], **model_attrs),
                        tokenizer_attrs=dict(**self.import_kwargs[-1], **tokenizer_attrs),
                        adapter_map=resolve_peft_config_type(adapter_map) if adapter_map is not None else None,
                        serialisation=serialisation,
                        local=_local,
                        prompt_template=prompt_template,
                        system_message=system_message,
                        llm_backend__=backend,
                        llm_config__=llm_config)

  @apply(lambda val: tuple(str.lower(i) if i else i for i in val))
  def _make_tag_components(self, model_id: str, model_version: str | None, backend: LiteralBackend) -> tuple[str, str]:
    '''Return a valid tag name (<backend>-<repo>--<model_id>) and its tag version.'''
    model_id, *maybe_revision = model_id.rsplit(':')
    if len(maybe_revision) > 0:
      if model_version is not None: logger.warning("revision is specified within 'model_id' (%s), and 'model_version=%s' will be ignored.", maybe_revision[0], model_version)
      model_version = maybe_revision[0]
    if validate_is_path(model_id): model_id, model_version = resolve_filepath(model_id), first_not_none(model_version, default=generate_hash_from_file(model_id))
    return f'{backend}-{normalise_model_name(model_id)}', model_version

  # yapf: disable
  def __setattr__(self,attr: str,value: t.Any)->None:
    if attr in _reserved_namespace:raise ForbiddenAttributeError(f'{attr} should not be set during runtime.')
    super().__setattr__(attr,value)
  @property
  def import_kwargs(self)->tuple[dict[str, t.Any],dict[str, t.Any]]: return {'device_map': 'auto' if torch.cuda.is_available() and torch.cuda.device_count() > 1 else None, 'torch_dtype': torch.float16 if torch.cuda.is_available() else torch.float32}, {'padding_side': 'left', 'truncation_side': 'left'}
  @property
  def adapters_mapping(self)->AdaptersMapping|None:return self._adapter_map
  @property
  def trust_remote_code(self)->bool:return first_not_none(openllm_core.utils.check_bool_env('TRUST_REMOTE_CODE',False),default=self.config['trust_remote_code'])
  @property
  def runner_name(self)->str:return f"llm-{self.config['start_name']}-runner"
  @property
  def model_id(self)->str:return self._model_id
  @property
  def revision(self)->str:return t.cast(str, self._revision)
  @property
  def tag(self)->bentoml.Tag:return self._tag
  @property
  def bentomodel(self)->bentoml.Model:return openllm.serialisation.get(self)
  @property
  def config(self)->LLMConfig:
    if self.__llm_config__ is None:self.__llm_config__=openllm.AutoConfig.infer_class_from_llm(self).model_construct_env(**self._model_attrs)
    return self.__llm_config__
  def save_pretrained(self) -> bentoml.Model: return openllm.import_model(self.config['start_name'], model_id=self.model_id, model_version=self._revision, backend=self.__llm_backend__, quantize=self._quantise)
  # NOTE: The section below defines a loose contract with langchain's LLM interface.
  @property
  def llm_type(self)->str:return normalise_model_name(self._model_id)
  @property
  def identifying_params(self)->DictStrAny: return {'configuration': self.config.model_dump_json().decode(),'model_ids': orjson.dumps(self.config['model_ids']).decode(),'model_id': self.model_id}
  @property
  def llm_parameters(self)->tuple[tuple[tuple[t.Any,...],DictStrAny],DictStrAny]:return (self._model_decls,self._model_attrs),self._tokenizer_attrs
  def sanitize_parameters(self, prompt: str, **attrs: t.Any) -> tuple[str, DictStrAny, DictStrAny]:
    return self.config.sanitize_parameters(prompt,prompt_template=self._prompt_template,system_message=self._system_message,**attrs)
  def postprocess_generate(self, prompt: str, generation_result: t.Any, **attrs: t.Any) -> t.Any:
    if isinstance(generation_result, dict) and 'text' in generation_result: return generation_result['text']
    return self.config.postprocess_generate(prompt, generation_result, **attrs)
  # yapf: enable

  @property
  def model(self) -> M:
    if self.__llm_model__ is None:
      model = openllm.serialisation.load_model(self, *self._model_decls, **self._model_attrs)
      # If OOM, then it is probably you don't have enough VRAM to run this model.
      if self.__llm_backend__ == 'pt' and is_torch_available():
        loaded_in_kbit = getattr(model, 'is_loaded_in_8bit', False) or getattr(model, 'is_loaded_in_4bit', False) or getattr(model, 'is_quantized', False)
        if torch.cuda.is_available() and torch.cuda.device_count() == 1 and not loaded_in_kbit and not isinstance(model, transformers.Pipeline):
          try:
            model = model.to('cuda')
          except Exception as err:
            raise OpenLLMException(
                f'Failed to load {self} into GPU: {err}\nTip: If you run into OOM issue, maybe try different offload strategy. See https://huggingface.co/docs/transformers/v4.31.0/en/main_classes/quantization#offload-between-cpu-and-gpu for more information.'
            ) from err
      self.__llm_model__ = model
    return self.__llm_model__

  @property
  def tokenizer(self) -> T:
    # NOTE: the signature of load_tokenizer here is the wrapper under _wrapped_load_tokenizer
    if self.__llm_tokenizer__ is None: self.__llm_tokenizer__ = openllm.serialisation.load_tokenizer(self, **self.llm_parameters[-1])
    return self.__llm_tokenizer__

  @property
  def runner(self) -> LLMRunner[M, T]:
    if self.__llm_runner__ is None: self.__llm_runner__ = _RunnerFactory(self)
    return self.__llm_runner__

  async def generate(self,
                     prompt: str,
                     stop: str | t.Iterable[str] | None = None,
                     stop_token_ids: list[int] | None = None,
                     request_id: str | None = None,
                     format_prompt: bool = True,
                     adapter_name: str | None = None,
                     **attrs: t.Any) -> GenerationOutput:
    attrs.pop('return_type', None)  # We don't need return type here, since we will always return objects
    if isinstance(self.runner._runner_handle, DummyRunnerHandle):
      if os.getenv('BENTO_PATH') is not None: raise RuntimeError('Runner client failed to set up correctly.')
      else: self.runner.init_local(quiet=True)

    if format_prompt: prompt, *_ = self.sanitize_parameters(prompt, **attrs)
    config = self.config.model_construct_env(**attrs)

    if stop_token_ids is None: stop_token_ids = []
    if self.tokenizer.eos_token_id not in stop_token_ids: stop_token_ids.append(self.tokenizer.eos_token_id)
    if stop is None: stop = set()
    elif isinstance(stop, str): stop = {stop}
    else: stop = set(stop)
    for tid in stop_token_ids:
      if tid: stop.add(self.tokenizer.decode(tid))

    prompt_token_ids = self.tokenizer.encode(prompt)
    if request_id is None: request_id = openllm_core.utils.gen_random_uuid()
    async for out in self.runner.generate.async_stream(prompt_token_ids, request_id, stop=stop, adapter_name=adapter_name, **config.model_dump()):
      pass
    return GenerationOutput.from_sse(out).with_options(prompt=prompt)

  @t.overload
  async def generate_iterator(self,
                              prompt: str,
                              stop: str | t.Iterable[str] | None = ...,
                              stop_token_ids: list[int] | None = ...,
                              request_id: str | None = ...,
                              return_type: t.Literal['object', 'token'] = ...,
                              format_prompt: bool = ...,
                              adapter_name: str | None = ...,
                              **attrs: t.Any) -> t.AsyncGenerator[GenerationOutput, None]:
    ...

  @t.overload
  async def generate_iterator(self,
                              prompt: str,
                              stop: str | t.Iterable[str] | None = ...,
                              stop_token_ids: list[int] | None = ...,
                              request_id: str | None = ...,
                              return_type: t.Literal['text', 'sse'] = ...,
                              format_prompt: bool = ...,
                              adapter_name: str | None = ...,
                              **attrs: t.Any) -> t.AsyncGenerator[str, None]:
    ...

  async def generate_iterator(self,
                              prompt: str,
                              stop: str | t.Iterable[str] | None = None,
                              stop_token_ids: list[int] | None = None,
                              request_id: str | None = None,
                              return_type: InferenceReturnType = 'object',
                              format_prompt: bool = True,
                              adapter_name: str | None = None,
                              **attrs: t.Any) -> t.AsyncGenerator[GenerationOutput | str, None]:
    if return_type not in {'text', 'sse', 'object', 'token'}: raise ValueError(f"'return_type' can only be one of ['token', 'text', 'sse'], while '{return_type}' is given.")
    if isinstance(self.runner._runner_handle, DummyRunnerHandle):
      if os.getenv('BENTO_PATH') is not None: raise RuntimeError('Runner client failed to set up correctly.')
      else: self.runner.init_local(quiet=True)

    if format_prompt: prompt, *_ = self.sanitize_parameters(prompt, **attrs)
    config = self.config.model_construct_env(**attrs)

    if stop_token_ids is None: stop_token_ids = []
    if self.tokenizer.eos_token_id not in stop_token_ids: stop_token_ids.append(self.tokenizer.eos_token_id)
    if stop is None: stop = set()
    elif isinstance(stop, str): stop = {stop}
    else: stop = set(stop)
    for tid in stop_token_ids:
      if tid: stop.add(self.tokenizer.decode(tid))

    prompt_token_ids = self.tokenizer.encode(prompt)
    if request_id is None: request_id = openllm_core.utils.gen_random_uuid()
    previous_texts, previous_num_tokens = [''] * config['n'], [0] * config['n']
    async for out in self.runner.generate_iterator.async_stream(prompt_token_ids, request_id, stop=stop, adapter_name=adapter_name, **config.model_dump()):
      if return_type == 'sse': yield out
      else:
        generated = GenerationOutput.from_sse(out).with_options(prompt=prompt)
        if generated.finished: break
        if return_type == 'object': yield generated
        else:
          for output in generated.outputs:
            i = output.index
            delta_text = output.text[len(previous_texts[i]):]
            delta_token = output.token_ids[previous_num_tokens[i]:]
            previous_texts[i] = output.text
            previous_num_tokens[i] = len(output.token_ids)
            if return_type == 'text': yield delta_text
            else:
              delta_outputs = [None] * len(generated.outputs)
              delta_outputs[i] = output.with_options(text=delta_text, token_ids=delta_token)
              yield generated.with_options(outputs=delta_outputs)
            if output.finish_reason is not None: break

def Runner(model_name: str,
           ensure_available: bool = False,
           init_local: bool = False,
           backend: LiteralBackend | None = None,
           llm_config: LLMConfig | None = None,
           **attrs: t.Any) -> LLMRunner[t.Any, t.Any]:
  '''Create a Runner for given LLM. For a list of currently supported LLM, check out 'openllm models'.

  > [!WARNING]
  > This method is now deprecated and in favor of 'openllm.LLM.runner'

  ```python
  runner = openllm.Runner("dolly-v2")

  @svc.on_startup
  def download():
    runner.download_model()
  ```

  if `init_local=True` (For development workflow), it will also enable `ensure_available`.
  Default value of `ensure_available` is None. If set then use that given value, otherwise fallback to the aforementioned behaviour.

  Args:
    model_name: Supported model name from 'openllm models'
    ensure_available: If True, it will download the model if it is not available. If False, it will skip downloading the model.
                      If False, make sure the model is available locally.
    backend: The given Runner implementation one choose for this Runner. If `OPENLLM_BACKEND` is set, it will respect it.
    llm_config: Optional ``openllm.LLMConfig`` to initialise this ``openllm.LLMRunner``.
    init_local: If True, it will initialize the model locally. This is useful if you want to run the model locally. (Symmetrical to bentoml.Runner.init_local())
    **attrs: The rest of kwargs will then be passed to the LLM. Refer to the LLM documentation for the kwargs behaviour
  '''
  if llm_config is None: llm_config = openllm.AutoConfig.for_model(model_name)
  model_id = attrs.get('model_id') or llm_config['env']['model_id_value']
  msg = f'''\
Using 'openllm.Runner' is now deprecated. Make sure to switch to the following syntax:

```python
llm = openllm.LLM('{model_id}')

svc = bentoml.Service('...', runners=[llm.runner])

@svc.api(...)
async def chat(input: str) -> str:
  await llm.generate(input)
```
  '''
  warnings.warn(msg, DeprecationWarning, stacklevel=3)
  attrs.update({
      'model_id': model_id,
      'quantize': llm_config['env']['quantize_value'],
      'serialisation': first_not_none(attrs.get('serialisation'), os.environ.get('OPENLLM_SERIALIZATION'), default=llm_config['serialisation']),
      'system_message': first_not_none(os.environ.get('OPENLLM_SYSTEM_MESSAGE'), attrs.get('system_message'), None),
      'prompt_template': first_not_none(os.environ.get('OPENLLM_PROMPT_TEMPLATE'), attrs.get('prompt_template'), None),
  })

  backend = t.cast(LiteralBackend, first_not_none(backend, default='vllm' if is_vllm_available() else 'pt'))
  if init_local: ensure_available = True
  llm = LLM(backend=backend, llm_config=llm_config, **attrs)
  if ensure_available: llm.save_pretrained()
  if init_local: llm.runner.init_local(quiet=True)
  return llm.runner

def _RunnerFactory(self: openllm.LLM[M, T],
                   /,
                   models: list[bentoml.Model] | None = None,
                   max_batch_size: int | None = None,
                   max_latency_ms: int | None = None,
                   scheduling_strategy: type[bentoml.Strategy] = CascadingResourceStrategy,
                   *,
                   backend: LiteralBackend | None = None) -> LLMRunner[M, T]:
  backend = t.cast(LiteralBackend, first_not_none(backend, os.environ.get('OPENLLM_BACKEND'), default=self.__llm_backend__))

  models = models if models is not None else []
  try:
    models.append(self.bentomodel)
  except bentoml.exceptions.NotFound as err:
    raise RuntimeError(f'Failed to locate {self.bentomodel}:{err}') from err

  if backend == 'vllm':
    from ._runners import vLLMRunnable as OpenLLMRunnable
  else:
    from ._runners import PyTorchRunnable as OpenLLMRunnable
  if self._prompt_template: prompt_template = self._prompt_template.to_string()
  elif hasattr(self.config, 'default_prompt_template'): prompt_template = self.config.default_prompt_template
  else: prompt_template = None
  if self._system_message: system_message = self._system_message
  elif hasattr(self.config, 'default_system_message'): system_message = self.config.default_system_message
  else: system_message = None

  # yapf: disable
  def _wrapped_repr_keys(_: LLMRunner[M, T]) -> set[str]: return {'config', 'llm_type', 'runner_methods', 'backend', 'llm_tag'}
  def _generate_sync(_: LLMRunner[M, T], prompt: str, **kwargs: t.Any) -> t.Any:
    # NOTE: This should only be used with LangChain
    async def infer(): return await self.generate(prompt, **kwargs)
    return asyncio.run(infer())
  def _wrapped_repr_args(_: LLMRunner[M, T]) -> ReprArgs:
    yield 'runner_methods', {method.name: {'batchable': method.config.batchable, 'batch_dim': method.config.batch_dim if method.config.batchable else None} for method in _.runner_methods}
    yield 'config', self.config.model_dump(flatten=True)
    yield 'llm_type', _.llm_type
    yield 'backend', backend
    yield 'llm_tag', self.tag
  # yapf: enable

  return types.new_class(self.__class__.__name__ + 'Runner', (bentoml.Runner,),
                         exec_body=lambda ns: ns.update({
                             'llm_type': self.llm_type,
                             'identifying_params': self.identifying_params,
                             'llm_tag': self.tag,
                             'llm': self,
                             'config': self.config,
                             'backend': backend,
                             'download_model': self.save_pretrained,
                             '__module__': self.__module__,
                             '__call__': _generate_sync,
                             '__doc__': self.config['env'].start_docstring,
                             '__repr__': ReprMixin.__repr__,
                             '__repr_keys__': property(_wrapped_repr_keys),
                             '__repr_args__': _wrapped_repr_args,
                             'has_adapters': self.adapters_mapping is not None,
                             'prompt_template': prompt_template,
                             'system_message': system_message,
                         }))(OpenLLMRunnable,
                             name=self.runner_name,
                             embedded=False,
                             models=models,
                             max_batch_size=max_batch_size,
                             max_latency_ms=max_latency_ms,
                             scheduling_strategy=scheduling_strategy,
                             runnable_init_params=dict(llm=self),
                             method_configs=converter.unstructure({
                                 'generate': ModelSignature.from_dict(ModelSignatureDict(batchable=False)),
                                 'generate_iterator': ModelSignature.from_dict(ModelSignatureDict(batchable=False))
                             }))

__all__ = ['LLMRunner', 'LLMRunnable', 'Runner', 'LLM']
