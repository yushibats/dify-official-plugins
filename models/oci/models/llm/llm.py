import os
import base64
import copy
import json
import logging
import mimetypes
from collections.abc import Generator
from typing import Optional, Union
import requests
import oci
from dify_plugin.entities.model.llm import LLMResult, LLMResultChunk, LLMResultChunkDelta
from dify_plugin.entities.model.message import (
    AssistantPromptMessage,
    PromptMessage,
    PromptMessageContentType,
    PromptMessageTool,
    SystemPromptMessage,
    ToolPromptMessage,
    UserPromptMessage,
)
from dify_plugin.errors.model import (
    CredentialsValidateFailedError,
    InvokeAuthorizationError,
    InvokeBadRequestError,
    InvokeConnectionError,
    InvokeError,
    InvokeRateLimitError,
    InvokeServerUnavailableError,
)
from dify_plugin.interfaces.model.large_language_model import LargeLanguageModel
from oci.generative_ai_inference.models.base_chat_response import BaseChatResponse

logger = logging.getLogger(__name__)
request_template = {
    "compartmentId": "",
    "servingMode": {"modelId": "cohere.command-r-plus-08-2024", "servingType": "ON_DEMAND"},
    "chatRequest": {
        "apiFormat": "COHERE",
        "maxTokens": 600,
        "isStream": False,
        "temperature": 1,
        "topP": 0.75,
    },
}
oci_config_template = {
    "user": "",
    "fingerprint": "",
    "tenancy": "",
    "region": "",
    "compartment_id": "",
    "key_content": "",
}


class OCILargeLanguageModel(LargeLanguageModel):
    CONTEXT_TOKEN_LIMIT: int = int(os.getenv("OCI_LLM_CONTEXT_TOKENS", 2000))
    _supported_models = {
        "meta.llama-3.1-405b-instruct": {
            "system": True,
            "multimodal": False,
            "tool_call": False,
            "stream_tool_call": False,
        },
        "meta.llama-3.2-90b-vision-instruct": {
            "system": True,
            "multimodal": True,
            "tool_call": False,
            "stream_tool_call": False,
        },
        "meta.llama-3.3-70b-instruct": {
            "system": True,
            "multimodal": False,
            "tool_call": False,
            "stream_tool_call": False,
        },
        "meta.llama-4-maverick-17b-128e-instruct-fp8": {
            "system": True,
            "multimodal": True,
            "tool_call": True,
            "stream_tool_call": True,
        },
        "meta.llama-4-scout-17b-16e-instruct": {
            "system": True,
            "multimodal": True,
            "tool_call": True,
            "stream_tool_call": True,
        },
        "cohere.command-r-08-2024": {
            "system": True,
            "multimodal": False,
            "tool_call": True,
            "stream_tool_call": False,
        },
        "cohere.command-r-plus-08-2024": {
            "system": True,
            "multimodal": False,
            "tool_call": True,
            "stream_tool_call": False,
        },
        "xai.grok-3": {
            "system": True,
            "multimodal": False,
            "tool_call": True,
            "stream_tool_call": True,
        },
       "xai.grok-3-mini": {
            "system": True,
            "multimodal": False,
            "tool_call": True,
            "stream_tool_call": True,
        },
       "xai.grok-3-fast": {
            "system": True,
            "multimodal": False,
            "tool_call": True,
            "stream_tool_call": True,
        },
       "xai.grok-3-mini-fast": {
            "system": True,
            "multimodal": False,
            "tool_call": True,
            "stream_tool_call": True,
        },
        "gemini-2.5-pro": {
            "system": True,
            "multimodal": False,
            "tool_call": True,
            "stream_tool_call": True,
        },
    }

    def _is_tool_call_supported(self, model_id: str, stream: bool = False) -> bool:
        feature = self._supported_models.get(model_id)
        if not feature:
            return False
        return feature["stream_tool_call"] if stream else feature["tool_call"]

    def _is_multimodal_supported(self, model_id: str) -> bool:
        feature = self._supported_models.get(model_id)
        if not feature:
            return False
        return feature["multimodal"]

    def _is_system_prompt_supported(self, model_id: str) -> bool:
        feature = self._supported_models.get(model_id)
        if not feature:
            return False
        return feature["system"]

    def _invoke(
        self,
        model: str,
        credentials: dict,
        prompt_messages: list[PromptMessage],
        model_parameters: dict,
        tools: Optional[list[PromptMessageTool]] = None,
        stop: Optional[list[str]] = None,
        stream: bool = True,
        user: Optional[str] = None,
    ) -> Union[LLMResult, Generator]:
        """
        Invoke large language model

        :param model: model name
        :param credentials: model credentials
        :param prompt_messages: prompt messages
        :param model_parameters: model parameters
        :param tools: tools for tool calling
        :param stop: stop words
        :param stream: is stream response
        :param user: unique user id
        :return: full response or stream response chunk generator result
        """
        return self._generate(model, credentials, prompt_messages, model_parameters, tools, stop, stream, user)

    def get_num_tokens(
        self,
        model: str,
        credentials: dict,
        prompt_messages: list[PromptMessage],
        tools: Optional[list[PromptMessageTool]] = None,
    ) -> int:
        """
        Get number of tokens for given prompt messages

        :param model: model name
        :param credentials: model credentials
        :param prompt_messages: prompt messages
        :param tools: tools for tool calling
        :return:md = genai.GenerativeModel(model)
        """
        prompt = self._convert_messages_to_prompt(prompt_messages)
        return self._get_num_tokens_by_gpt2(prompt)

    def get_num_characters(
        self,
        model: str,
        credentials: dict,
        prompt_messages: list[PromptMessage],
        tools: Optional[list[PromptMessageTool]] = None,
    ) -> int:
        """
        Get number of tokens for given prompt messages

        :param model: model name
        :param credentials: model credentials
        :param prompt_messages: prompt messages
        :param tools: tools for tool calling
        :return:md = genai.GenerativeModel(model)
        """
        prompt = self._convert_messages_to_prompt(prompt_messages)
        return len(prompt)

    def _convert_messages_to_prompt(self, messages: list[PromptMessage]) -> str:
        """
        :param messages: List of PromptMessage to combine.
        :return: Combined string with necessary human_prompt and ai_prompt tags.
        """
        messages = messages.copy()
        text = "".join((self._convert_one_message_to_text(message) for message in messages))
        return text.rstrip()

    def validate_credentials(self, model: str, credentials: dict) -> None:
        """
        Validate model credentials

        :param model: model name
        :param credentials: model credentials
        :return:
        """
        try:
            ping_message = SystemPromptMessage(content="ping")
            self._generate(model, credentials, [ping_message], {"maxTokens": 5})
        except Exception as ex:
            raise CredentialsValidateFailedError(str(ex))

    def _generate(
        self,
        model: str,
        credentials: dict,
        prompt_messages: list[PromptMessage],
        model_parameters: dict,
        tools: Optional[list[PromptMessageTool]] = None,
        stop: Optional[list[str]] = None,
        stream: bool = True,
        user: Optional[str] = None,
    ) -> Union[LLMResult, Generator]:
        """
        Invoke large language model

        :param model: model name
        :param credentials: credentials kwargs
        :param prompt_messages: prompt messages
        :param model_parameters: model parameters
        :param stop: stop words
        :param stream: is stream response
        :param user: unique user id
        :return: full response or stream response chunk generator result
        """
        oci_config = copy.deepcopy(oci_config_template)
        if "oci_config_content" in credentials:
            oci_config_content = base64.b64decode(credentials.get("oci_config_content")).decode("utf-8")
            config_items = oci_config_content.split("/")
            if len(config_items) != 5:
                raise CredentialsValidateFailedError(
                    "oci_config_content should be base64.b64encode('user_ocid/fingerprint/tenancy_ocid/region/compartment_ocid'.encode('utf-8'))"
                )
            oci_config["user"] = config_items[0]
            oci_config["fingerprint"] = config_items[1]
            oci_config["tenancy"] = config_items[2]
            oci_config["region"] = config_items[3]
            oci_config["compartment_id"] = config_items[4]
        else:
            raise CredentialsValidateFailedError("need to set oci_config_content in credentials ")
        if "oci_key_content" in credentials:
            oci_key_content = base64.b64decode(credentials.get("oci_key_content")).decode("utf-8")
            oci_config["key_content"] = oci_key_content.encode(encoding="utf-8")
        else:
            raise CredentialsValidateFailedError("need to set oci_config_content in credentials ")
        compartment_id = oci_config["compartment_id"]
        client = oci.generative_ai_inference.GenerativeAiInferenceClient(config=oci_config)
        request_args = copy.deepcopy(request_template)
        request_args["compartmentId"] = compartment_id
        request_args["servingMode"]["modelId"] = model
        stop = model_parameters.pop("stop", None)
        if stop:
            if model.startswith("cohere"):
                request_args["chatRequest"]["stop_sequences"] = stop  # Cohere: stop_sequences
            elif model.startswith("meta") or model.startswith("xai"):
                request_args["chatRequest"]["stop"] = stop  # Meta/XAI: stop
            else:
                logger.warning(f"Model '{model}' does not support stop sequences. Ignored.")
        chat_history = []
        system_prompts = []
        request_args["chatRequest"]["maxTokens"] = model_parameters.pop("maxTokens", 600)
        if model in ("xai.grok-3-mini-fast", "xai.grok-3-mini", "xai.grok-4"):
            safe_model_parameters = {
                "temperature": model_parameters.get("temperature", 1),
                "topP": model_parameters.get("topP", 0.75),
            }
            frequency_penalty = 0
            presence_penalty = 0
        else:
            safe_model_parameters = {
                "frequencyPenalty": model_parameters.get("frequencyPenalty", 0),
                "presencePenalty": model_parameters.get("presencePenalty", 0),
                "temperature": model_parameters.get("temperature", 1),
                "topP": model_parameters.get("topP", 0.75),
            }
            frequency_penalty = safe_model_parameters["frequencyPenalty"]
            presence_penalty = safe_model_parameters["presencePenalty"]

        request_args["chatRequest"].update(safe_model_parameters)
        if frequency_penalty > 0 and presence_penalty > 0:
            raise InvokeBadRequestError("Cannot set both frequency penalty and presence penalty")
        valid_value = self._is_tool_call_supported(model, stream)
        if tools is not None and len(tools) > 0:
            if not valid_value:
                raise InvokeBadRequestError("Does not support function calling")
        if model.startswith("cohere"):
            for message in prompt_messages[:-1]:
                text = ""
                if isinstance(message.content, str):
                    text = message.content
                if isinstance(message, UserPromptMessage):
                    chat_history.append({"role": "USER", "message": text})
                else:
                    chat_history.append({"role": "CHATBOT", "message": text})
                if isinstance(message, SystemPromptMessage):
                    if isinstance(message.content, str):
                        system_prompts.append(message.content)
            args = {
                "apiFormat": "COHERE",
                "preambleOverride": " ".join(system_prompts),
                "message": prompt_messages[-1].content,
                "chatHistory": chat_history,
            }
            request_args["chatRequest"].update(args)
        elif model.startswith("meta"):
            from typing import cast
            from dify_plugin.entities.model.message import (
                PromptMessageContentType,
                TextPromptMessageContent,
                ImagePromptMessageContent,
            )

            # まず全ての画像 URL を集めて「最後の画像」を選定
            all_images = []
            for msg in prompt_messages:
                if isinstance(msg.content, list):
                    for mc in msg.content:
                        if mc.type == PromptMessageContentType.IMAGE:
                            all_images.append(cast(ImagePromptMessageContent, mc).data)
            last_image_url = all_images[-1] if all_images else None
            used_image = False  # 画像使用フラグ

            meta_messages = []
            for message in prompt_messages:
                sub_messages = []

                # (A) もし content が list なら、その中身をループ
                if isinstance(message.content, list):
                    for mc in message.content:
                        if mc.type == PromptMessageContentType.TEXT:
                            txt = cast(TextPromptMessageContent, mc).data
                            sub_messages.append({"type": "TEXT", "text": txt})

                        elif mc.type == PromptMessageContentType.IMAGE:
                            img_data = cast(ImagePromptMessageContent, mc).data

                            # 1枚目の画像のみ処理、以降はスキップ
                            if used_image or img_data != last_image_url:
                                continue
                            used_image = True

                            # data URI ならそのまま、そうでなければダウンロード／Base64
                            if img_data.startswith("data:"):
                                img_url = img_data
                            else:
                                try:
                                    if img_data.startswith(("http://", "https://")):
                                        resp = requests.get(img_data)
                                        resp.raise_for_status()
                                        mime_type = (
                                            resp.headers.get("Content-Type")
                                            or mimetypes.guess_type(img_data)[0]
                                            or "image/png"
                                        )
                                        img_bytes = resp.content
                                    else:
                                        with open(img_data, "rb") as f:
                                            img_bytes = f.read()
                                        mime_type = mimetypes.guess_type(img_data)[0] or "image/png"
                                    base64_data = base64.b64encode(img_bytes).decode("utf-8")
                                    img_url = f"data:{mime_type};base64,{base64_data}"
                                except Exception as exc:
                                    raise InvokeBadRequestError(f"Failed to load image: {exc}")

                            sub_messages.append({
                                "type": "IMAGE",
                                "imageUrl": {"url": img_url, "detail": mc.detail.value},
                            })

                # (B) content が文字列（TEXT）の場合
                else:
                    sub_messages.append({"type": "TEXT", "text": message.content})

                # TEXT／IMAGE が１つも入っていないメッセージはスキップ
                if not sub_messages:
                    continue

                # ロール名とともに配列に追加
                meta_messages.append({
                    "role": message.role.name,
                    "content": sub_messages,
                })

            # それでも空になってしまったら、最後のテキストだけを強制的に入れるフォールバック
            if not meta_messages and prompt_messages:
                last = prompt_messages[-1]
                meta_messages.append({
                    "role": last.role.name,
                    "content": [{"type": "TEXT", "text": last.content}],
                })

            args = {
                "apiFormat": "GENERIC",
                "messages": meta_messages,
                "numGenerations": 1,
                "topK": -1,
            }
            request_args["chatRequest"].update(args)
        elif model.startswith("xai"):
            xai_messages = []
            for message in prompt_messages:
                text = message.content
                xai_messages.append({"role": message.role.name, "content": [{"type": "TEXT", "text": text}]})
            args = {"apiFormat": "GENERIC","messages": xai_messages,"numGenerations": 1,"topK": -1,}
            request_args["chatRequest"].update(args)
        elif model.startswith("gemini"):
            xai_messages = []
            for message in prompt_messages:
                text = message.content
                xai_messages.append({"role": message.role.name, "content": [{"type": "TEXT", "text": text}]})
            args = {"apiFormat": "GENERIC","messages": xai_messages,"numGenerations": 1,"topK": -1,}
            request_args["chatRequest"].update(args)
        if stream:
            request_args["chatRequest"]["isStream"] = True
        response = client.chat(request_args)
        if stream:
            return self._handle_generate_stream_response(model, credentials, response, prompt_messages)
        return self._handle_generate_response(model, credentials, response, prompt_messages)

    def _handle_generate_response(
        self, model: str, credentials: dict, response: BaseChatResponse, prompt_messages: list[PromptMessage]
    ) -> LLMResult:
        """
        Handle llm response

        :param model: model name
        :param credentials: credentials
        :param response: response
        :param prompt_messages: prompt messages
        :return: llm response
        """
        assistant_prompt_message = AssistantPromptMessage(content=response.data.chat_response.text)
        prompt_tokens = self.get_num_characters(model, credentials, prompt_messages)
        completion_tokens = self.get_num_characters(model, credentials, [assistant_prompt_message])
        usage = self._calc_response_usage(model, credentials, prompt_tokens, completion_tokens)
        result = LLMResult(model=model, prompt_messages=prompt_messages, message=assistant_prompt_message, usage=usage)
        return result

    def _handle_generate_stream_response(
            self, model: str, credentials: dict, response: BaseChatResponse, prompt_messages: list[PromptMessage]
        ) -> Generator:
            """
            Handle llm stream response

            :param model: model name
            :param credentials: credentials
            :param response: response
            :param prompt_messages: prompt messages
            :return: llm response chunk generator result
            """
            index = -1
            events = response.data.events()
            full_text = ""
            
            for stream in events:
                chunk = json.loads(stream.data)
                
                if "finishReason" not in chunk:
                    assistant_prompt_message = AssistantPromptMessage(content="")
                    
                    if model.startswith("cohere"):
                        if chunk.get("text"):
                            assistant_prompt_message.content = chunk["text"]
                            full_text += chunk["text"]
                    elif model.startswith("meta"):
                        if chunk.get("message", {}).get("content", [{}])[0].get("text"):
                            text = chunk["message"]["content"][0]["text"]
                            assistant_prompt_message.content = text
                            full_text += text
                    elif model.startswith("xai"):
                        if chunk.get("message", {}).get("content", [{}])[0].get("text"):
                            text = chunk["message"]["content"][0]["text"]
                            assistant_prompt_message.content = text
                            full_text += text
                    elif model.startswith("gemini"):
                        if chunk.get("message", {}).get("content", [{}])[0].get("text"):
                            text = chunk["message"]["content"][0]["text"]
                            assistant_prompt_message.content = text
                            full_text += text
                    
                    if assistant_prompt_message.content:
                        index += 1
                        yield LLMResultChunk(
                            model=model,
                            prompt_messages=prompt_messages,
                            delta=LLMResultChunkDelta(index=index, message=assistant_prompt_message),
                        )
                else:
                    prompt_tokens = self.get_num_characters(model, credentials, prompt_messages)
                    completion_tokens = self.get_num_characters(model, credentials, [AssistantPromptMessage(content=full_text)])
                    usage = self._calc_response_usage(model, credentials, prompt_tokens, completion_tokens)
                    yield LLMResultChunk(
                        model=model,
                        prompt_messages=prompt_messages,
                        delta=LLMResultChunkDelta(
                            index=index,
                            message=AssistantPromptMessage(content=""),
                            finish_reason=str(chunk["finishReason"]),
                            usage=usage,
                        ),
                    )
            
    def _convert_one_message_to_text(self, message: PromptMessage) -> str:
        """
        Convert a single message to a string.

        :param message: PromptMessage to convert.
        :return: String representation of the message.
        """
        human_prompt = "\n\nuser:"
        ai_prompt = "\n\nmodel:"
        content = message.content
        if isinstance(content, list):
            content = "".join((c.data for c in content if c.type != PromptMessageContentType.IMAGE))
        if isinstance(message, UserPromptMessage):
            message_text = f"{human_prompt} {content}"
        elif isinstance(message, AssistantPromptMessage):
            message_text = f"{ai_prompt} {content}"
        elif isinstance(message, SystemPromptMessage | ToolPromptMessage):
            message_text = f"{human_prompt} {content}"
        else:
            raise ValueError(f"Got unknown type {message}")
        return message_text

    @property
    def _invoke_error_mapping(self) -> dict[type[InvokeError], list[type[Exception]]]:
        """
        Map model invoke error to unified error
        The key is the error type thrown to the caller
        The value is the error type thrown by the model,
        which needs to be converted into a unified error type for the caller.

        :return: Invoke error mapping
        """
        return {
            InvokeConnectionError: [],
            InvokeServerUnavailableError: [],
            InvokeRateLimitError: [],
            InvokeAuthorizationError: [],
            InvokeBadRequestError: [],
        }
