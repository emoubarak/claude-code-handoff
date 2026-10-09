"""LiteLLM proxy callback that applies ``compat.normalize`` to every request.

In a LiteLLM proxy config::

    litellm_settings:
      callbacks: claude_handoff.litellm_callback.handler

``claude_handoff`` must be importable by the LiteLLM process (``pip install`` this package in its environment).
"""

from litellm.integrations.custom_logger import CustomLogger

from .compat import normalize


class ClaudeHandoff(CustomLogger):
    async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type):
        return normalize(data)


handler = ClaudeHandoff()
