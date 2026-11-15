import os
import sys
from pathlib import Path
from dotenv import load_dotenv
from typing import Annotated, TypedDict

from langchain_core.messages import BaseMessage, SystemMessage
from langgraph.graph import StateGraph, START, END
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode, tools_condition
from langgraph.checkpoint.memory import MemorySaver

# ==========================================
# 1. SETUP & PATH RESOLUTION
# ==========================================
script_dir = Path(__file__).resolve().parent
project_root = script_dir.parents[0]

if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from src.agent_tools import query_telemetry_db, fetch_corridor_conditions, search_compliance_sop

load_dotenv(project_root / ".env")

class AgentState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]

# ==========================================
# 2. FACTORY INITIALIZATION: AGENT REASONER LLM
# ==========================================

_default_llm = "GEMINI" if (os.getenv("GEMINI_API_KEY") or os.getenv("GEMINI_APKEY")) else "OLLAMA"
AGENT_LLM_SETTING = (os.getenv("Agent_llm") or os.getenv("llm") or _default_llm).strip().upper()

if AGENT_LLM_SETTING == "OPENAI":
    print("🤖 Brain Mode: Utilizing Cloud OpenAI Reasoner (gpt-4o)...")
    from langchain_openai import ChatOpenAI
    llm = ChatOpenAI(model="gpt-4o", temperature=0)

elif AGENT_LLM_SETTING == "GEMINI":
    gemini_model = os.getenv("GEMINI_MODEL", "gemini-3.8-flash").strip()
    print(f"✨ Brain Mode: Utilizing Google Gemini Cloud Reasoner ({gemini_model})...")
    from langchain_google_genai import ChatGoogleGenerativeAI

    gemini_api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GEMINI_APKEY")
    if not gemini_api_key:
        raise ValueError("CRITICAL: Ensure GEMINI_API_KEY is present in your active .env profile.")

    llm = ChatGoogleGenerativeAI(
        model=gemini_model,  # gemini-3.8-flash, gemini-3.8-pro
        temperature=0,
        google_api_key=gemini_api_key,
        max_output_tokens=8192,
    )

else:  # FALLBACK / DEFAULT RUNNER MODE
    print("🤗 Brain Mode: Local Fallback Activated. Binding Local Ollama (qwen2.5:7b)...")
    try:
        from langchain_ollama import ChatOllama
    except ImportError:
        try:
            from langchain_community.chat_models.ollama import ChatOllama
        except ImportError as exc:
            raise ImportError(
                "Local Ollama fallback requires `langchain-ollama` (pip install langchain-ollama) "
                "or set `Agent_llm=GEMINI` and `GEMINI_API_KEY` in your .env file."
            ) from exc
    llm = ChatOllama(model="qwen2.5:7b", temperature=0, num_predict=1024)

fde_tools = [query_telemetry_db, fetch_corridor_conditions, search_compliance_sop]
llm_with_tools = llm.bind_tools(fde_tools)

# Load the business-structured system prompt dynamically so prompt edits take effect immediately
prompt_path = project_root / "src" / "prompts" / "system_prompt.txt"


def _load_system_prompt() -> SystemMessage:
    try:
        return SystemMessage(content=prompt_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        print(f"Warning: Could not find {prompt_path}, using fallback system prompt.")
        return SystemMessage(content="You are a helpful AI assistant.")


SYSTEM_PROMPT = _load_system_prompt()


def _format_message_content(content) -> str:
    """Normalizes string or multi-block list content (e.g. from Gemini 2.5) into clean text."""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and item.get("type") == "text":
                parts.append(item.get("text", ""))
        return "\n".join(p for p in parts if p).strip()
    return str(content).strip()


# ==========================================
# 3. GRAPH ARCHITECTURE ASSEMBLY
# ==========================================
def reasoning_node(state: AgentState):
    messages = list(state.get("messages", []))
    current_sys_prompt = _load_system_prompt()

    # Ensure the latest SystemMessage is always at index 0 for every thread (CLI, Streamlit, or tests)
    if not messages:
        messages = [current_sys_prompt]
    elif isinstance(messages[0], SystemMessage):
        messages[0] = current_sys_prompt
    else:
        messages = [current_sys_prompt] + messages

    # Guard against invoking Gemini with only a SystemMessage (no user/tool contents)
    has_non_system_msg = any(not isinstance(m, SystemMessage) for m in messages)
    if not has_non_system_msg:
        return {"messages": []}

    response = llm_with_tools.invoke(messages)
    return {"messages": [response]}

print("⚙️ Compiling LangGraph FDE Orchestrator...")
graph_builder = StateGraph(AgentState)
graph_builder.add_node("reasoner", reasoning_node)
graph_builder.add_node("tools", ToolNode(fde_tools))

graph_builder.add_edge(START, "reasoner")
graph_builder.add_conditional_edges("reasoner", tools_condition)
graph_builder.add_edge("tools", "reasoner")

fde_agent = graph_builder.compile(checkpointer=MemorySaver())

# ==========================================
# 4. CHAT LOOP TESTING PANEL
# ==========================================
if __name__ == "__main__":
    print("\n" + "="*55)
    print("🚀 FDE Supply Chain Orchestrator State Machine Online")
    print(f"   Configured Execution: [LLM: {AGENT_LLM_SETTING}] -> [Embeddings: {os.getenv('Embeddings_model', 'LOCAL')}]")
    print("="*55 + "\n")
    
    thread_config = {"configurable": {"thread_id": "production_test_1"}}
    fde_agent.update_state(thread_config, {"messages": [SYSTEM_PROMPT]})
    
    while True:
        try:
            user_input = input("\nDispatcher > ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nExiting orchestrator.")
            break

        if not user_input:
            continue
        if user_input.lower() in ["exit", "quit"]:
            break
            
        try:
            events = fde_agent.stream({"messages": [("user", user_input)]}, config=thread_config, stream_mode="updates")
            for event in events:
                for node_name, node_state in event.items():
                    if node_name == "tools":
                        print("   [System] 🔄 Retrieving external data elements via ToolNode...")
                    elif node_name == "reasoner":
                        msgs = node_state.get("messages", [])
                        if msgs:
                            text_out = _format_message_content(msgs[-1].content)
                            if text_out:
                                print(f"\n🤖 FDE Agent:\n{text_out}")
        except Exception as e:
            err_str = str(e)
            if "402" in err_str or "RESOURCE_EXHAUSTED" in err_str:
                print(
                    "\n❌ [Gemini API Quota/Billing Error]: Your Gemini API key has depleted its prepaid credits (HTTP 402).\n"
                    "   -> Fix 1: Generate a Free-Tier API key at https://aistudio.google.com/apikey and update GEMINI_API_KEY in .env\n"
                    "   -> Fix 2: Top up credits at https://ai.studio/projects\n"
                    "   -> Fix 3: Switch Agent_llm=OLLAMA in .env to run locally."
                )
            else:
                print(f"\n❌ [Orchestrator Runtime Error]: {err_str}")