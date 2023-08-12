from __future__ import annotations
import typing as t
from collections import OrderedDict
from .configuration_auto import CONFIG_MAPPING_NAMES
from .factory import BaseAutoLLMClass, _LazyAutoMapping
if t.TYPE_CHECKING:
  import transformers, openllm
  from collections import OrderedDict

MODEL_MAPPING_NAMES = OrderedDict([("chatglm", "ChatGLM"), ("dolly_v2", "DollyV2"), ("falcon", "Falcon"), ("flan_t5", "FlanT5"), ("gpt_neox", "GPTNeoX"), ("llama", "Llama"), ("mpt", "MPT"), ("opt", "OPT"), ("stablelm", "StableLM"), ("starcoder", "StarCoder"), ("baichuan", "Baichuan")])
MODEL_MAPPING: OrderedDict[t.Type[openllm.LLMConfig], t.Type[openllm.LLM["transformers.PreTrainedModel", "transformers.PreTrainedTokenizer"]]] = _LazyAutoMapping(CONFIG_MAPPING_NAMES, MODEL_MAPPING_NAMES)
class AutoLLM(BaseAutoLLMClass):
  _model_mapping = MODEL_MAPPING
