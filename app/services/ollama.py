import requests
import json

# Define the Ollama server URL
OLLAMA_URL = "http://127.0.0.1:11434/api/chat"

# Define your query and model
model_name = "gpt-oss:20b"


# Initialize the conversation history
history = [{"role": "system", "content": """You answer questions."""}]

# Function to send a request to the model and get a response
def chat_with_model(prompt):
    global history
    # Add the user's input to the history
    history.append({"role": "user", "content": prompt})

    # Create the payload with the conversation history
    payload = {
        "model": model_name,  # Specify the model name here
        "messages": history
    }

    # Send the request to the Ollama server
    response = requests.post(OLLAMA_URL, json=payload, stream=True)

    # Combine the streamed response into one output
    if response.status_code == 200:
        full_response = ""
        for line in response.iter_lines():
            if line:  # Filter out keep-alive new lines
                try:
                    data = json.loads(line)
                    content = data.get("message", {}).get("content", "")
                    full_response += content  # Append content to the full response
                except json.JSONDecodeError:
                    print("\nError decoding line:", line)

        # Add the model's response to the history
        history.append({"role": "assistant", "content": full_response})

        # Return the model's full response
        return full_response
    else:
        print("\nError:", response.status_code, response.text)
        return None

# Chat loop
def start_chat():
    print("\nChat with the model! Type 'exit' to quit.\n")
    while True:
        user_input = input("You: ")
        if user_input.lower() == "exit":
            print("\nExiting chat. Goodbye!")
            break
        response = chat_with_model(user_input)
        if response:
            print("\nModel:", response)


start_chat()
