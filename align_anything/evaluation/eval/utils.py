# Copyright 2024 PKU-Alignment Team. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================

from typing import List
from openai import OpenAI
from concurrent.futures import ThreadPoolExecutor
import json
import os
import logging
from dataclasses import dataclass
import time
import openai
from hashlib import sha256
from openai.types.chat.chat_completion import ChatCompletion, ChatCompletionMessage, Choice, CompletionUsage

def create_openai_chat_completion(client : OpenAI,messages: List[dict], parameters: dict):
    response = client.chat.completions.create(
        messages=messages,
        model=parameters['model'],
        max_tokens=parameters.get('max_tokens', 2048),
        temperature=parameters.get('temperature', 1),
        top_p=parameters.get('top_p', 1),
        frequency_penalty=parameters.get('frequency_penalty', 0),
        presence_penalty=parameters.get('presence_penalty', 0),
        stop=parameters.get('stop', None),
        stream=parameters.get('stream', False),
        logprobs=parameters.get('logprobs', False),
        top_logprobs=parameters.get('top_logprobs', None),
    )
    return response


def batch_request_openai(
    type: str,
    inputs: List,
    model: str,
    num_workers: int = 1 , 
    cache_dir: str = "./.cache",
    openai_api_keys: str = None,
    openai_base_url: str = None,
    kwargs: dict = {},
    MAX_RETRY_STEPS = 5
) -> List:
    
    def generate_key(input):
        key = f"{type}_{model}_{input}_{kwargs}_{openai_api_keys}_{openai_base_url}"
        return sha256(str(key).encode()).hexdigest()
    
    def load_cache(key: str):
        cache_path = os.path.join(cache_dir, f"{key}.txt")
        if os.path.exists(cache_path):
            with open(cache_path, "r") as f:
                return eval(f.read())
        return None
    def write_cache(key: str, response):
        cache_path = os.path.join(cache_dir, f"{key}.txt")
        with open(cache_path, "w") as f:
            f.write(str(response))
        return None

    # Load environment variables
    if openai_api_keys is None:
        openai_api_keys = os.getenv("OPENAI_API_KEY")
    if openai_base_url is None:
        openai_base_url = os.getenv("OPENAI_API_BASE_URL")
    parameters = {
        'model': model,
    }
    parameters.update(kwargs)
    client = OpenAI(api_key=openai_api_keys, base_url=openai_base_url)

    logging.log(logging.INFO, f"Start batch request, type: {type}, model: {model}, num_workers: {num_workers}")
    print("Input_len",len(inputs))
    logging.log(logging.INFO, f"openai_api_keys: {openai_api_keys}, openai_base_url: {openai_base_url}")
    
    cache_dict = {}
    full_keys = [generate_key(input) for input in inputs]

    # Load cache
    if cache_dir is not None:
        for key in full_keys:
            response = load_cache(key)
            if response is not None:
                cache_dict[key] = response


    # Filter out cached inputs
    with ThreadPoolExecutor(max_workers=num_workers) as executor:
        futures = []
        for id, key, input in zip(range(len(inputs)), full_keys, inputs):
            if key in cache_dict:
                continue
            future = executor.submit(create_openai_chat_completion, client, input, parameters)
            futures.append((key,future))

        print('Appending all todo futures',len(futures))
        for id, (key, future) in enumerate(futures):
            retry_steps = 0
            while retry_steps < MAX_RETRY_STEPS:
                try:
                    response = future.result()
                    print(f"succeed {id}th request")
                    break
                except Exception as e:
                    response = {'error' : e}
                    retry_steps += 1
                    print(f"Retry {id}th request, retry_steps: {retry_steps}, error: {e}")
                    time.sleep(1)
                    future = executor.submit(create_openai_chat_completion, client, input, parameters)
                    continue
            # key = generate_key(input)
            cache_dict[key] = response
            print(response)

            # Save cache
            if cache_dir is not None:
                write_cache(key, response)
                    
            print(f"Finish {id}th request")
    print('Finished all requests')
    outputs = [cache_dict[key] for key in full_keys]
    return outputs

def clean_cache(cache_dir: str):
    for file in os.listdir(cache_dir):
        os.remove(os.path.join(cache_dir, file))
    return None

if __name__ == "__main__":
    outputs = batch_request_openai(
        type="Arena",
        model= 'deepseek-chat',
        inputs=[
            [{'role': 'system', 'content': 'judge which response is better' + str(EEE) + 'A or B'},
            {'role': 'user', 'content': '[A]This is a test prompt[B]This is another test prompt'}] for EEE in range(10)
        ],
        num_workers=8,
        openai_api_keys = "sk-4177a6705d084829b148e96cfef6ed15",
        openai_base_url = "https://api.deepseek.com/v1",
        cache_dir="./cache",
    )
    print(outputs)
    for output in outputs:
        print(output)