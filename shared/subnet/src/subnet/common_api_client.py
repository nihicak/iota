from abc import abstractmethod
import asyncio
from aiohttp import ClientSession, ClientTimeout
from loguru import logger
from substrateinterface.keypair import Keypair

from common import settings as common_settings
from common.settings import ORCHESTRATOR_HOST, ORCHESTRATOR_PORT, ORCHESTRATOR_SCHEMA
from common.utils.epistula import create_message_body, generate_header
from common.utils.exceptions import APIException, RateLimitException
from common.utils.partitions import MinerPartition

HEADER_REQUEST_ID = "X-Request-Id"


class CommonAPIClient:
    """
    Common API client for all entities.
    """

    @classmethod
    async def orchestrator_request(
        cls, method: str, path: str, body: dict | None = None, hotkey: Keypair | None = None
    ) -> dict:
        logger.opt(colors=True).debug(
            f"\n<magenta>Making orchestrator request | method: {method} | path: {path}</magenta>"
        )

        headers = None
        request_id = None  # Will be extracted from response
        body_bytes = create_message_body(data={} if not body else body)

        if hotkey:
            headers = generate_header(hotkey, body_bytes)
            # Don't add request ID to headers - let orchestrator generate it

        for i in range(common_settings.REQUEST_RETRY_COUNT):
            try:
                if i:
                    logger.warning(f"Retrying request to endpoint {path} (attempt {i + 1})")

                timeout = ClientTimeout(total=common_settings.CLIENT_REQUEST_TIMEOUT)
                async with ClientSession(timeout=timeout) as session:
                    async with session.request(
                        method,
                        f"{ORCHESTRATOR_SCHEMA}://{ORCHESTRATOR_HOST}:{ORCHESTRATOR_PORT}{path}",
                        json=body,
                        headers=headers,
                    ) as response:
                        # Extract request ID from response headers
                        request_id = response.headers.get(HEADER_REQUEST_ID, "unknown")
                        response_text = None

                        # Add request ID to logger context for all subsequent logs
                        with logger.contextualize(request_id=request_id):
                            if response.status == 429:
                                logger.warning(f"Rate limited on request to endpoint {path}")
                                await asyncio.sleep(2**i)
                                continue

                            if response.status != 200:
                                # Handle non-JSON error responses first
                                response_text = await response.text() if not response_text else response_text
                                msg = f"{response.status} - {response_text}"
                                raise APIException(f"Error making orchestrator request to endpoint {path}: {msg}")

                            # Success.
                            response_json = await response.json()
                            logger.debug(
                                f"Successfully completed request to {path}; response: {str(response_json)[:100]}"
                            )
                            return response_json

            except Exception:
                raise

        # The only time you get here is because you've exhausted all retries.
        error_msg = (
            f"Failed request after {common_settings.REQUEST_RETRY_COUNT} attempts: {response.status}, {response_text}"
        )
        if request_id:
            with logger.contextualize(request_id=request_id):
                logger.error(error_msg)
        raise RateLimitException(error_msg)

    @classmethod
    async def check_orchestrator_health(cls, hotkey: Keypair) -> bool | dict:
        try:
            response = await cls.orchestrator_request(method="GET", path="/healthcheck", hotkey=hotkey)
            if "error_name" in response:
                return response
            return response
        except Exception as e:
            logger.exception(f"Error checking orchestrator health: {e}")
            raise e

    @abstractmethod
    async def get_merged_partitions(self, hotkey: Keypair) -> list[MinerPartition] | dict:
        pass

    @abstractmethod
    async def get_num_splits(self) -> int | dict:
        pass
