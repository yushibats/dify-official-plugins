import time
from json import dumps
from typing import Optional
from dify_plugin import TextEmbeddingModel
from dify_plugin.entities.model import EmbeddingInputType, PriceType
from dify_plugin.entities.model.text_embedding import EmbeddingUsage, TextEmbeddingResult
from dify_plugin.errors.model import (
    CredentialsValidateFailedError,
    InvokeAuthorizationError,
    InvokeBadRequestError,
    InvokeConnectionError,
    InvokeError,
    InvokeRateLimitError,
    InvokeServerUnavailableError,
)
from requests import post
from models.llm.errors import (
    BadRequestError,
    InsufficientAccountBalanceError,
    InternalServerError,
    InvalidAPIKeyError,
    InvalidAuthenticationError,
    RateLimitReachedError,
)


class MinimaxTextEmbeddingModel(TextEmbeddingModel):
    """
    Model class for Minimax text embedding model.
    """

    def _invoke(
        self,
        model: str,
        credentials: dict,
        texts: list[str],
        user: Optional[str] = None,
        input_type: EmbeddingInputType = EmbeddingInputType.DOCUMENT,
    ) -> TextEmbeddingResult:
        """
        Invoke text embedding model

        :param model: model name
        :param credentials: model credentials
        :param texts: texts to embed
        :param user: unique user id
        :param input_type: input type
        :return: embeddings result
        """
        api_key = credentials["minimax_api_key"]
        group_id = credentials["minimax_group_id"]

        # Get endpoint_url from credentials, use default if not provided
        endpoint_url = credentials.get("endpoint_url", "https://api.minimax.chat/")
        base_url = endpoint_url.rstrip('/')

        if model != "embo-01":
            raise ValueError("Invalid model name")
        if not api_key:
            raise CredentialsValidateFailedError("api_key is required")
        url = f"{base_url}/v1/embeddings?GroupId={group_id}"
        headers = {"Authorization": "Bearer " + api_key, "Content-Type": "application/json"}
        embedding_type = "db" if input_type == EmbeddingInputType.DOCUMENT else "query"
        data = {"model": "embo-01", "texts": texts, "type": embedding_type}
        try:
            response = post(url, headers=headers, data=dumps(data))
        except Exception as e:
            raise InvokeConnectionError(str(e))
        if response.status_code != 200:
            raise InvokeServerUnavailableError(response.text)
        try:
            resp = response.json()
            if resp["base_resp"]["status_code"] != 0:
                code = resp["base_resp"]["status_code"]
                msg = resp["base_resp"]["status_msg"]
                self._handle_error(code, msg)
            embeddings = resp["vectors"]
            total_tokens = resp["total_tokens"]
        except InvalidAuthenticationError:
            raise InvalidAPIKeyError("Invalid api key")
        except KeyError as e:
            raise InternalServerError(f"Failed to convert response to json: {e} with text: {response.text}")
        usage = self._calc_response_usage(model=model, credentials=credentials, tokens=total_tokens)
        result = TextEmbeddingResult(model=model, embeddings=embeddings, usage=usage)
        return result

    def get_num_tokens(self, model: str, credentials: dict, texts: list[str]) -> list[int]:
        """
        Get number of tokens for given prompt messages

        :param model: model name
        :param credentials: model credentials
        :param texts: texts to embed
        :return:
        """
        tokens = []
        for text in texts:
            tokens.append(self._get_num_tokens_by_gpt2(text))
        return tokens

    def validate_credentials(self, model: str, credentials: dict) -> None:
        """
        Validate model credentials

        :param model: model name
        :param credentials: model credentials
        :return:
        """
        try:
            self._invoke(model=model, credentials=credentials, texts=["ping"])
        except InvalidAPIKeyError:
            raise CredentialsValidateFailedError("Invalid api key")

    def _handle_error(self, code: int, msg: str):
        if code in {1000, 1001}:
            raise InternalServerError(msg)
        elif code == 1002:
            raise RateLimitReachedError(msg)
        elif code == 1004:
            raise InvalidAuthenticationError(msg)
        elif code == 1008:
            raise InsufficientAccountBalanceError(msg)
        elif code == 2013:
            raise BadRequestError(msg)
        else:
            raise InternalServerError(msg)

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
            InvokeServerUnavailableError: [InternalServerError],
            InvokeRateLimitError: [RateLimitReachedError],
            InvokeAuthorizationError: [InvalidAuthenticationError, InsufficientAccountBalanceError, InvalidAPIKeyError],
            InvokeBadRequestError: [BadRequestError, KeyError],
        }

    def _calc_response_usage(self, model: str, credentials: dict, tokens: int) -> EmbeddingUsage:
        """
        Calculate response usage

        :param model: model name
        :param credentials: model credentials
        :param tokens: input tokens
        :return: usage
        """
        input_price_info = self.get_price(
            model=model, credentials=credentials, price_type=PriceType.INPUT, tokens=tokens
        )
        usage = EmbeddingUsage(
            tokens=tokens,
            total_tokens=tokens,
            unit_price=input_price_info.unit_price,
            price_unit=input_price_info.unit,
            total_price=input_price_info.total_amount,
            currency=input_price_info.currency,
            latency=time.perf_counter() - self.started_at,
        )
        return usage
