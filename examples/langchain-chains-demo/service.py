from __future__ import annotations
import typing as t

from langchain.chains import LLMChain
from langchain.llms import OpenLLM
from langchain.prompts import PromptTemplate
from pydantic import BaseModel

import bentoml
from bentoml.io import JSON, Text


class Query(BaseModel):
  industry: str
  product_name: str
  keywords: t.List[str]
  llm_config: t.Dict[str, t.Any]


def gen_llm(model_name: str, model_id: str | None = None, **attrs: t.Any) -> OpenLLM:
  return OpenLLM(model_name=model_name, model_id=model_id, embedded=False, **attrs)


llm = gen_llm('llama', model_id='TheBloke/Llama-2-13B-chat-GPTQ', quantize='gptq')

prompt = PromptTemplate(
  input_variables=['industry', 'product_name', 'keywords'],
  template="""
You are a Facebook Ads Copywriter with a strong background in persuasive
writing and marketing. You craft compelling copy that appeals to the target
audience's emotions and needs, peruading them to take action or make a
purchase. You are given the following context to create a facebook ad copy.
It should provide an attention-grabbing headline optimizied for capivating
leads and perusaive calls to action.

Industry: {industry}
Product: {product_name}
Keywords: {keywords}
Facebook Ads copy:
    """,
)
chain = LLMChain(llm=llm, prompt=prompt)

svc = bentoml.Service('fb-ads-copy', runners=[llm.runner])

SAMPLE_INPUT = Query(
  industry='SAAS',
  product_name='BentoML',
  keywords=['open source', 'developer tool', 'AI application platform', 'serverless', 'cost-efficient'],
  llm_config=llm.config.model_dump(),
)


@svc.api(input=JSON.from_sample(sample=SAMPLE_INPUT), output=Text())
def generate(query: Query):
  return chain.run(
    {'industry': query.industry, 'product_name': query.product_name, 'keywords': ', '.join(query.keywords)}
  )
