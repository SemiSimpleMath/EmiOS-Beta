import app.assistant.tests.test_setup  # This is just run for the import
from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.utils.pydantic_classes import Message

def main():
    # Create the boundary agent
    agent = DI.agent_factory.create_agent("knowledge_graph_add::conversation_boundary")
    if not agent:
        print("❌ Agent creation failed!")
        return

    # Create the exact conversation that caused the issue
    # This is the conversation about chat companions and Squid Game
    # Format as plain dictionaries (exactly like in the pipeline)
    messages = [
        {
            "id": "msg_0",
            "role": "user", 
            "message": "nah i am just chatting. i like to have a chat companion too not always an assistant",
            "timestamp": None
        },
        {
            "id": "msg_1",
            "role": "assistant",
            "message": "I totally get that! It's nice to just have a friendly chat sometimes. So, what's been catching your interest lately? Any new hobbies or shows you've been into?",
            "timestamp": None
        },
        {
            "id": "msg_2",
            "role": "user",
            "message": "I started watching squid game season 2",
            "timestamp": None
        },
        {
            "id": "msg_3",
            "role": "assistant",
            "message": "That's awesome! Squid Game Season 2 has been highly anticipated. How are you finding it so far? Any favorite moments or characters?",
            "timestamp": None
        },
        {
            "id": "msg_4",
            "role": "user",
            "message": "It has pretty interesting villains, but one of them just died in an unremarkable way so that was kinda weirdly dissappointing.",
            "timestamp": None
        },
        {
            "id": "msg_5",
            "role": "assistant",
            "message": "I can see how that would be disappointing, especially if the villain was intriguing. Sometimes shows take unexpected turns! Are there any other characters or plot twists that have caught your attention?",
            "timestamp": None
        },
        {
            "id": "msg_6",
            "role": "user",
            "message": "they have split the season in two parts. I guess we have to wait for season 3 to see how this one ends.",
            "timestamp": None
        },
        {
            "id": "msg_7",
            "role": "assistant",
            "message": "It sounds like they're really keeping the suspense going by splitting the season! I guess that means more excitement to look forward to in Season 3. Do you think they'll wrap up the story in a satisfying way?",
            "timestamp": None
        },
        {
            "id": "msg_8",
            "role": "user",
            "message": "for season 2...i doubt it.",
            "timestamp": None
        },
        {
            "id": "msg_9",
            "role": "assistant",
            "message": "I get what you mean. Sometimes when a season is split, it can feel like the story is left hanging. Hopefully, they'll tie everything together in Season 3. Have you seen any other shows that did something similar?",
            "timestamp": None
        }
    ]

    print("=== TESTING BOUNDARY AGENT ===")
    print(f"Input: {len(messages)} messages")
    print("\n--- MESSAGES ---")
    for i, msg in enumerate(messages):
        print(f"{i}: {msg['role']}: {msg['message'][:100]}...")
    
    print("\n--- CALLING BOUNDARY AGENT ---")
    
    # Convert to the format expected by the agent (exactly like in pipeline)
    agent_input = {
        "messages": messages,
        "analysis_window_size": len(messages)
    }
    
    msg = Message(agent_input=agent_input)
    result = agent.action_handler(msg)
    
    print(f"\n=== BOUNDARY AGENT OUTPUT ===")
    print(f"Result: {result}")
    
    if result and result.data:
        data = result.data
        print(f"\n--- ANALYSIS SUMMARY ---")
        print(f"Summary: {data.get('analysis_summary', 'No summary provided')}")
        
        print(f"\n--- MESSAGE BOUNDS ---")
        message_bounds = data.get('message_bounds', [])
        print(f"Found {len(message_bounds)} message bounds:")
        
        for i, bound in enumerate(message_bounds):
            print(f"\n{i+1}. Message ID: {bound.get('message_id', 'Unknown')}")
            bounds = bound.get('bounds', {})
            print(f"   Start: {bounds.get('start_message_id', 'Unknown')}")
            print(f"   End: {bounds.get('end_message_id', 'Unknown')}")
            print(f"   Should Process: {bounds.get('should_process', 'Unknown')}")
            print(f"   Reasoning: {bounds.get('reasoning', 'No reasoning provided')}")
        
        # Analyze the results
        print(f"\n--- ANALYSIS ---")
        should_process_count = sum(1 for bound in message_bounds 
                                 if bound.get('bounds', {}).get('should_process', False))
        print(f"Messages marked for processing: {should_process_count}/{len(message_bounds)}")
        
        if should_process_count == 0:
            print("🚨 ISSUE FOUND: No messages marked for processing!")
            print("   This would trigger the fallback logic and create massive context windows.")
        elif should_process_count < len(message_bounds):
            print("⚠️  PARTIAL ISSUE: Some messages not marked for processing")
        else:
            print("✅ All messages marked for processing")
            
        # Check for conversation boundaries
        unique_conversations = set()
        for bound in message_bounds:
            bounds = bound.get('bounds', {})
            if bounds.get('should_process', False):
                start_id = bounds.get('start_message_id', '')
                end_id = bounds.get('end_message_id', '')
                unique_conversations.add((start_id, end_id))
        
        print(f"Unique conversation spans: {len(unique_conversations)}")
        for i, (start, end) in enumerate(unique_conversations, 1):
            print(f"   {i}. {start} -> {end}")
            
    else:
        print("❌ No result data returned from boundary agent")

if __name__ == "__main__":
    main()
