"""
Response generation node with remote configuration support.
Generates final responses using retrieved context - customize for your response style.
"""

from typing import Dict, Any
from langchain_core.messages import HumanMessage, AIMessage

from src.config.agent_config import get_node_configuration
from src.utils.llm_provider import get_llm_model, get_provider_from_model
from src.utils.langfuse_decorator import trace_node
from cams_otel_lib import Logger as logger, otel_trace

# FEATURE:prometheus
try:
    from src.utils.metrics import track_node_metrics, get_metrics_collector
    _METRICS_AVAILABLE = True
except ImportError:
    def track_node_metrics(name):
        def decorator(func): return func
        return decorator
    _METRICS_AVAILABLE = False


class ResponseGeneratorNode:
    """
    Node that generates final responses using LLM and retrieved context.

    Supports node-level configuration from remote registry API:
    - Model selection and parameters
    - Prompt templates and response style
    - Temperature and other generation settings
    - Multi-provider support (OpenAI, Anthropic, Google)
    """

    @otel_trace
    def __init__(self, config: Dict[str, Any], tenant_id: str = "default"):
        """Initialize response generation node with tenant-aware configuration."""
        self.global_config = config
        self.tenant_id = tenant_id

        # Get node-specific configuration
        self.node_config = get_node_configuration("response", tenant_id, config)

        # Get tenant-specific LLM configuration
        tenant_config = config.get(tenant_id, config.get("default", {}))
        llm_config = tenant_config.get("llm_config", {}).get("response", {})
        self.model = llm_config.get("model", "gpt-4o")
        self.temperature = llm_config.get("temperature", 0.3)
        self.max_tokens = llm_config.get("max_tokens")  # None → provider default
        self.provider = llm_config.get("provider")

        # Infer provider from model name if not specified
        if not self.provider:
            try:
                self.provider = get_provider_from_model(self.model)
            except ValueError:
                logger.warning(f"Could not infer provider for model {self.model}, defaulting to openai")
                self.provider = "openai"

        logger.debug(f"ResponseNode initialized for tenant {tenant_id} with provider={self.provider}, model={self.model}")

    # FEATURE:prometheus
    @track_node_metrics("response_generator")
    @trace_node(
        node_name="response_generator",
        capture_input=True,
        capture_output=True,
        estimate_tokens=True,
        calculate_cost=True,
        metadata_fn=lambda state: {
            "context_length": len(state.get("retrieved_context", "")),
            "num_messages": len(state.get("messages", [])),
            "has_context": bool(state.get("retrieved_context")),
            "tenant_id": state.get("tenant_id", "default"),
        },
    )
    @otel_trace
    async def execute(self, state: Dict[str, Any]) -> Dict[str, Any]:
        """
        Generate final response using context.

        Customize this method to:
        - Change response generation strategy
        - Add post-processing steps
        - Implement different formatting
        """
        messages = state.get("messages", [])
        retrieved_context = state.get("retrieved_context", "")

        # Get the last user message
        if not messages:
            return {"final_response": "I don't see any question to answer."}

        last_message = messages[-1]
        user_question = (
            last_message.content
            if hasattr(last_message, "content")
            else str(last_message)
        )

        # Get tenant-specific config
        tenant_config = self.global_config.get(
            state.get("tenant_id", "default"), self.global_config.get("default", {})
        )

        # Get domain knowledge for context
        domain_knowledge = tenant_config.get("domain_knowledge", {})
        thresholds = domain_knowledge.get("thresholds", {})
        rules = domain_knowledge.get("rules", [])
        categories = domain_knowledge.get("categories", [])
        examples = domain_knowledge.get("examples", [])

        # Format domain knowledge for prompt if available
        domain_context = ""
        if thresholds:
            domain_context += "\n\nDomain Knowledge - Thresholds:\n"
            for key, value in thresholds.items():
                domain_context += f"- {key}: {value}\n"

        if rules:
            domain_context += "\nDomain Rules:\n"
            for rule in rules:
                domain_context += f"- {rule}\n"

        if categories:
            domain_context += "\nKnowledge Categories:\n"
            for cat in categories:
                if isinstance(cat, dict):
                    domain_context += f"- {cat.get('name', '')}: {cat.get('description', '')}\n"
                else:
                    domain_context += f"- {cat}\n"

        if examples:
            domain_context += "\nExamples:\n"
            for ex in examples[:3]:  # cap at 3 to keep prompt size reasonable
                if isinstance(ex, dict):
                    q = ex.get("question", ex.get("input", ""))
                    a = ex.get("answer", ex.get("output", ""))
                    if q and a:
                        domain_context += f"Q: {q}\nA: {a}\n"
                else:
                    domain_context += f"- {ex}\n"

        # Add domain context to retrieved context
        full_context = f"{retrieved_context}{domain_context}".strip()

        # Get prompt template
        prompt_template = tenant_config.get("prompts", {}).get(
            "response",
            "Answer the user's question using the provided context.\n\nContext: {context}\n\nQuestion: {question}\n\nAnswer:",
        )

        # Format conversation history
        conversation_history = self._format_conversation(messages[:-1])

        # Format prompt with full context including domain knowledge
        prompt = prompt_template.format(
            context=full_context or "No specific context was retrieved.",
            conversation=conversation_history,
            question=user_question,
        )

        try:
            # Get custom headers from state for LiteLLM gateway
            custom_headers = state.get("litellm_headers")

            # Create LLM instance with custom headers
            llm = get_llm_model(
                provider=self.provider,
                model_name=self.model,
                temperature=self.temperature,
                streaming=False,
                max_tokens=self.max_tokens,
                custom_headers=custom_headers,
            )

            # Generate response (tracing handled automatically by decorator)
            import time as _time
            _llm_start = _time.time()
            response = await llm.ainvoke([HumanMessage(content=prompt)])
            _llm_duration = _time.time() - _llm_start
            final_response = response.content.strip()

            # FEATURE:prometheus
            if _METRICS_AVAILABLE:
                _mc = get_metrics_collector()
                _mc.track_llm_duration(self.provider, self.model, _llm_duration)
                _usage = getattr(response, "usage_metadata", None) or {}
                _in = _usage.get("input_tokens", 0)
                _out = _usage.get("output_tokens", 0)
                if _in or _out:
                    try:
                        from src.utils.cost_calculator import calculate_cost
                        _cost = calculate_cost(self.model, _in, _out)
                    except Exception:
                        _cost = 0.0
                    _mc.track_llm_tokens(self.provider, self.model, _in, _out, _cost, state.get("tenant_id", "default"))

            # Post-process response if needed
            final_response = self._post_process_response(final_response)

            logger.info(f"Generated response length: {len(final_response)}")

            return {
                "final_response": final_response,
                "messages": [AIMessage(content=final_response)],
            }

        except Exception as e:
            logger.error(f"Error generating response: {e}")
            return {
                "final_response": "I apologize, but I encountered an error generating a response.",
                "messages": messages,
            }

    @otel_trace
    def _format_conversation(self, messages: list) -> str:
        """Format conversation history for context."""
        if not messages:
            return "No previous conversation."

        formatted = []
        for msg in messages[-5:]:  # Last 5 messages
            if hasattr(msg, "content"):
                role = "User" if isinstance(msg, HumanMessage) else "Assistant"
                formatted.append(f"{role}: {msg.content}")

        return "\n".join(formatted) if formatted else "No previous conversation."

    @otel_trace
    def _post_process_response(self, response: str) -> str:
        """
        Post-process the generated response.

        Customize this to:
        - Clean up formatting
        - Add citations
        - Apply content filters
        - Add disclaimers
        """
        # Remove any leading/trailing whitespace
        response = response.strip()

        # Add any custom post-processing here

        return response
