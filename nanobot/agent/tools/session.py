"""Session tools for updating session metadata."""

from typing import Any, Optional

from nanobot.agent.tools.base import Tool
from nanobot.session.manager import SessionManager


class UpdateSessionMetadataTool(Tool):
    """Tool to update session metadata."""

    def __init__(self, session_manager: SessionManager):
        self.session_manager = session_manager
        self._channel: Optional[str] = None
        self._chat_id: Optional[str] = None

    def set_context(self, channel: str, chat_id: str) -> None:
        """Set the current session context."""
        self._channel = channel
        self._chat_id = chat_id

    @property
    def name(self) -> str:
        return "update_session_metadata"

    @property
    def description(self) -> str:
        return """Update session metadata including customer profile and CRM config."""

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                # Customer profile fields
                "website_domain": {
                    "type": "string",
                    "description": "Customer's website domain"
                },
                "brand_intro": {
                    "type": "string",
                    "description": "Brief introduction of the brand"
                },
                "business_type": {
                    "type": "string",
                    "description": "Type of business: B2B or B2C"
                },
                "contacts": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "role": {"type": "string"}
                        },
                        "required": ["name"]
                    },
                    "description": "List of contact persons"
                },
                # CRM config fields
                "uid": {
                    "type": "string",
                    "description": "Internal CRM system unique ID"
                },
                "roomid": {
                    "type": "string",
                    "description": "Customer service room ID (对接群id)"
                }
            }
        }

    async def execute(self, **kwargs: Any) -> str:
        if not self._channel or not self._chat_id:
            return "Error: No active session context"

        session_key = f"{self._channel}:{self._chat_id}"
        session = self.session_manager.get_or_create(session_key)

        updates = []

        # Initialize customer_profile if not exists
        if "customer_profile" not in session.metadata:
            session.metadata["customer_profile"] = {}

        # Update customer_profile fields
        customer_fields = ["website_domain", "brand_intro", "business_type", "contacts"]
        for field in customer_fields:
            if field in kwargs:
                session.metadata["customer_profile"][field] = kwargs[field]
                updates.append(f"customer_profile.{field}")

        # Initialize crm_config if not exists
        if "crm_config" not in session.metadata:
            session.metadata["crm_config"] = {}

        # Update crm_config fields
        crm_fields = ["uid", "roomid"]
        for field in crm_fields:
            if field in kwargs:
                session.metadata["crm_config"][field] = kwargs[field]
                updates.append(f"crm_config.{field}")

        if not updates:
            return "No updates provided."

        self.session_manager.save(session)
        return f"Successfully updated session metadata: {', '.join(updates)}"
