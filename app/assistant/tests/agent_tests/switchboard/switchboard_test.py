"""
Test the switchboard agent with conversation windows.
"""

import sys
import os

# Add project root to path
project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))))
sys.path.insert(0, project_root)

from app.assistant.tests.test_setup import initialize_services
from app.assistant.utils.pydantic_classes import Message


def test_switchboard_agent():
    """Test the switchboard agent with various conversation windows."""
    
    print("=" * 80)
    print("🧪 Testing Switchboard Agent")
    print("=" * 80)
    
    # Initialize services
    initialize_services()
    
    from app.assistant.ServiceLocator.service_locator import DI
    
    # Create switchboard agent
    agent = DI.agent_factory.create_agent('switchboard')
    if not agent:
        print("❌ Failed to create switchboard agent")
        return
    
    print("✅ Agent loaded successfully\n")
    
    # Test cases
    test_cases = [
        {
            "name": "Cold Stone + Thai Food (Multiple Preferences)",
            "messages": [
                {"role": "assistant", "message": "Alright, hit me with it! What's your Cold Stone masterpiece?"},
                {"role": "user", "message": "I like their coffee ice cream with heathbar crunch."},
                {"role": "assistant", "message": "That's a strong pick! Coffee ice cream plus Heath Bar crunch is basically rocket fuel for dessert lovers."},
                {"role": "assistant", "message": "Thai food never disappoints. Are you thinking about ordering in or cooking something at home?"},
                {"role": "user", "message": "Thai food sounds good."},
                {"role": "assistant", "message": "Ordering is definitely easier. Any favorites you're craving?"},
                {"role": "user", "message": "meh i am not that good of a cook. If we have thai food its definitely going to be ordered."},
            ],
            "expected_min_chunks": 2,  # Should find at least 2 preferences
            "expected_categories": ["preference"],
        },
        {
            "name": "Blueberries (Multi-turn Preference)",
            "messages": [
                {"role": "user", "message": "you know what I like?"},
                {"role": "assistant", "message": "what?"},
                {"role": "user", "message": "blueberries."},
                {"role": "assistant", "message": "That's great!"},
                {"role": "user", "message": "especially in smoothies."},
            ],
            "expected_min_chunks": 1,
            "expected_categories": ["preference"],
            "should_contain": ["blueberries", "smoothies"],
        },
        {
            "name": "No Preferences (General Chat)",
            "messages": [
                {"role": "user", "message": "What's the weather like today?"},
                {"role": "assistant", "message": "It's sunny and 72°F in Irvine."},
                {"role": "user", "message": "Thanks!"},
            ],
            "expected_min_chunks": 0,
            "expected_categories": [],
        },
        {
            "name": "Feedback (Changing Preference)",
            "messages": [
                {"role": "user", "message": "You know I used to like eggs for breakfast"},
                {"role": "assistant", "message": "Yes, I remember!"},
                {"role": "user", "message": "Well, I can't stand them anymore. Don't suggest eggs."},
            ],
            "expected_min_chunks": 1,
            "expected_categories": ["preference", "feedback"],
        },
        {
            "name": "Communication Preference",
            "messages": [
                {"role": "user", "message": "Speak to me in Spanish for a week."},
                {"role": "assistant", "message": "¡Claro que sí! I'll speak Spanish with you for the next week."},
            ],
            "expected_min_chunks": 1,
            "expected_categories": ["preference"],
            "should_contain": ["Spanish"],
        },
        {
            "name": "Work Routine Preference",
            "messages": [
                {"role": "user", "message": "Don't suggest coffee after 2pm, I can't sleep if I have caffeine late."},
                {"role": "assistant", "message": "Got it! No coffee suggestions after 2pm."},
            ],
            "expected_min_chunks": 1,
            "expected_categories": ["preference"],
            "should_contain": ["coffee", "2pm"],
        },
    ]
    
    passed = 0
    failed = 0
    
    for i, test in enumerate(test_cases):
        print(f"\n{'=' * 80}")
        print(f"TEST {i+1}/{len(test_cases)}: {test['name']}")
        print("=" * 80)
        
        # Show conversation
        print("\n📝 Conversation:")
        for msg in test["messages"]:
            print(f"   [{msg['role']}]: {msg['message'][:80]}{'...' if len(msg['message']) > 80 else ''}")
        
        # Run agent
        agent_input = {"messages": test["messages"]}
        response = agent.action_handler(Message(agent_input=agent_input))
        result = response.data or {}
        
        tagged_chunks = result.get('tagged_chunks', [])
        
        print(f"\n📊 Results:")
        print(f"   Chunks extracted: {len(tagged_chunks)}")
        
        for j, chunk in enumerate(tagged_chunks):
            category = chunk.get('category', 'none')
            summary = chunk.get('extracted_summary', '')
            confidence = chunk.get('confidence', 0.0)
            print(f"   Chunk {j+1}: [{category}] (conf: {confidence:.2f})")
            print(f"            \"{summary}\"")
        
        # Validate
        issues = []
        
        # Check minimum chunks
        if len(tagged_chunks) < test["expected_min_chunks"]:
            issues.append(f"Expected at least {test['expected_min_chunks']} chunks, got {len(tagged_chunks)}")
        
        # Check categories
        found_categories = set(c.get('category') for c in tagged_chunks if c.get('category'))
        expected_cats = set(test["expected_categories"])
        if expected_cats and not found_categories.intersection(expected_cats):
            issues.append(f"Expected categories {expected_cats}, got {found_categories}")
        
        # Check should_contain
        if "should_contain" in test:
            all_summaries = " ".join(c.get('extracted_summary', '') for c in tagged_chunks).lower()
            for term in test["should_contain"]:
                if term.lower() not in all_summaries:
                    issues.append(f"Expected summary to contain '{term}'")
        
        if issues:
            print(f"\n❌ FAIL - Issues:")
            for issue in issues:
                print(f"   • {issue}")
            failed += 1
        else:
            print(f"\n✅ PASS")
            passed += 1
    
    # Summary
    print("\n" + "=" * 80)
    print(f"📊 SUMMARY: {passed}/{len(test_cases)} tests passed")
    print("=" * 80)
    
    if failed > 0:
        print(f"❌ {failed} test(s) failed")
    else:
        print("✅ All tests passed!")


if __name__ == "__main__":
    test_switchboard_agent()
