import requests
import json

# Ollama server URL and model name
OLLAMA_URL = "http://127.0.0.1:11434/api/chat"
model_name = "gpt-oss:20b"

# System prompt enforcing structured JSON output
system_prompt = """
You are a helpful math tutor. Your goal is to guide the user through a solution step by step.
You MUST respond with a single, valid JSON object that adheres to the following schema. Do not include any other text or explanations.

{
    "steps": [
        {
            "explanation": "string - Your explanation of this step.",
            "output": "string - The resulting mathematical equation for this step."
        }
    ],
    "final_answer": "string - The final, simplified answer."
}
"""

# Initialize the conversation history
history = [{"role": "system", "content": system_prompt}]

def chat_with_model(prompt):
    global history
    history.append({"role": "user", "content": prompt})

    payload = {
        "model": model_name,
        "messages": history
    }

    response = requests.post(OLLAMA_URL, json=payload, stream=True)

    if response.status_code == 200:
        full_response = ""
        for line in response.iter_lines():
            if line:
                try:
                    data = json.loads(line)
                    content = data.get("message", {}).get("content", "")
                    full_response += content
                except json.JSONDecodeError:
                    print("\nError decoding line:", line)

        history.append({"role": "assistant", "content": full_response})

        return full_response
    else:
        print("\nError:", response.status_code, response.text)
        return None

def try_parse_json(response_text):
    try:
        return json.loads(response_text)
    except json.JSONDecodeError:
        print("\nWarning: model returned non-JSON response.")
        print(response_text)
        return None

def start_chat():
    print("\nChat with the model! Type 'exit' to quit.\n")
    while True:
        user_input = input("You: ")
        if user_input.lower() == "exit":
            print("\nExiting chat. Goodbye!")
            break

        raw = chat_with_model(user_input)
        if not raw:
            continue
        print(raw)
        parsed = try_parse_json(raw)

        if parsed:
            print("\nParsed response:")
            for i, step in enumerate(parsed.get("steps", []), 1):
                print(f"Step {i}: {step['explanation']}")
                print(f"       {step['output']}")
            print(f"\nFinal Answer: {parsed.get('final_answer')}")
        else:
            print("\nRaw model response (could not parse):")
            print(raw)

start_chat()
