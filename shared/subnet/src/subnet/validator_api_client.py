from common.models.api_models import ValidationTaskResponse, ValidatorRegistrationResponse, SubnetScores
from loguru import logger
from subnet.common_api_client import CommonAPIClient
from substrateinterface.keypair import Keypair
from common.utils.partitions import MinerPartition


class ValidatorAPIClient(CommonAPIClient):
    @classmethod
    async def register_validator_request(cls, hotkey: Keypair) -> ValidatorRegistrationResponse | dict:
        try:
            response = await cls.orchestrator_request(method="POST", path="/validator/register", hotkey=hotkey)
            if hasattr(response, "error_name"):
                return response

            return ValidatorRegistrationResponse(**response)
        except Exception as e:
            logger.error(f"Error registering validator: {e}")
            raise e

    @classmethod
    async def get_global_miner_scores(cls, hotkey: Keypair) -> SubnetScores | None:
        """Get the global scores for all miners from the orchestrator."""
        try:
            response: SubnetScores = await cls.orchestrator_request(
                method="GET", path="/validator/global_miner_scores", hotkey=hotkey
            )
            if hasattr(response, "error_name"):
                logger.error(f"Error getting global miner scores: {response}")
                return
            return response

        except Exception as e:
            logger.error(f"Error getting global miner scores: {e}")
            raise e

    @classmethod
    async def submit_miner_scores(cls, hotkey: Keypair, miner_scores: dict[str, dict[str, float | str]]) -> dict:
        """Submit the miner scores to the orchestrator.

        Args:
            hotkey (Keypair): The hotkey of the validator.
            miner_scores (dict[str, dict[str, float | str]]): The miner scores to submit. dict[str(uid) : dict{task_type : score, hotkey : str}]
        """
        try:
            response: dict = await cls.orchestrator_request(
                method="POST", path="/validator/submit_miner_scores", hotkey=hotkey, body=miner_scores
            )
            if hasattr(response, "error_name"):
                return response
            return response
        except Exception as e:
            logger.error(f"Error submitting miner scores: {e}")
            raise e

    @classmethod
    async def fetch_tasks(cls, hotkey: Keypair) -> dict:
        """Fetch a task from the orchestrator."""
        try:
            response: list[dict] = await cls.orchestrator_request(
                method="GET", path="/validator/fetch_tasks", hotkey=hotkey
            )
            if hasattr(response, "error_name"):
                return response
            return response
        except Exception as e:
            logger.error(f"Error fetching task from orchestrator: {e}")
            raise e

    @classmethod
    async def submit_task_result(cls, hotkey: Keypair, task_result: ValidationTaskResponse):
        try:
            response: dict = await cls.orchestrator_request(
                method="POST", path="/validator/submit_task_result", hotkey=hotkey, body=task_result.model_dump()
            )
            if hasattr(response, "error_name"):
                return response
            return response
        except Exception as e:
            logger.error(f"Error submitting task result to orchestrator: {e}")
            raise e

    async def get_merged_partitions(self, hotkey: Keypair) -> list[MinerPartition] | dict:
        try:
            response = await CommonAPIClient.orchestrator_request(
                method="GET", path="/common/get_merged_partitions", hotkey=hotkey
            )
            if "error_name" in response:
                return response
            return [MinerPartition(**partition) for partition in response]
        except Exception as e:
            logger.error(f"Error getting merged partitions: {e}")
            raise
