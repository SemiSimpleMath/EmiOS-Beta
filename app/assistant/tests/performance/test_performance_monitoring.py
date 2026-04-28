#!/usr/bin/env python3
"""
Test script for performance monitoring
Verifies that timing is working correctly across the system.
"""

import time
import sys
import os

# Add the app directory to the Python path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../..', '..'))

from app.assistant.performance.performance_monitor import performance_monitor
from app.assistant.performance.performance_analyzer import PerformanceAnalyzer
from app.assistant.performance.llm_performance_analyzer import LLMPerformanceAnalyzer

def test_basic_timing():
    """Test basic timing functionality."""
    print("🧪 Testing basic timing functionality...")
    
    # Test simple timing
    timer_id = performance_monitor.start_timer('test_operation', 'test_001')
    time.sleep(0.1)  # Simulate work
    duration = performance_monitor.end_timer(timer_id, {'status': 'success'})
    
    print(f"   ✅ Basic timing test: {duration:.3f}s")
    
    # Test multiple operations
    for i in range(3):
        timer_id = performance_monitor.start_timer('test_operation', f'test_{i+2:03d}')
        time.sleep(0.05 * (i + 1))  # Increasing sleep time
        performance_monitor.end_timer(timer_id, {'status': 'success', 'iteration': i})
    
    print("   ✅ Multiple operations test completed")

def test_llm_simulation():
    """Simulate LLM calls with different durations."""
    print("🤖 Testing LLM call simulation...")
    
    # Simulate different types of LLM calls
    llm_scenarios = [
        ('llm_structured_output', 'gpt-4.1-2025-04-14_5', 2.5),
        ('llm_structured_output', 'gpt-4.1-2025-04-14_15', 8.0),
        ('llm_structured_output_json', 'gpt-4o-2024-08-06_3', 1.5),
        ('llm_structured_output_json', 'gpt-4o-2024-08-06_25', 12.0),
    ]
    
    for operation, request_id, duration in llm_scenarios:
        timer_id = performance_monitor.start_timer(operation, request_id)
        time.sleep(duration)
        performance_monitor.end_timer(timer_id, {
            'status': 'success',
            'model': request_id.split('_')[0],
            'message_count': int(request_id.split('_')[1])
        })
        print(f"   ✅ Simulated {operation}: {duration:.1f}s")
    
    # Simulate some errors
    timer_id = performance_monitor.start_timer('llm_structured_output', 'error_test')
    time.sleep(0.5)
    performance_monitor.end_timer(timer_id, {
        'status': 'error',
        'model': 'gpt-4.1-2025-04-14',
        'message_count': 10,
        'error': 'Rate limit exceeded'
    })
    print("   ✅ Simulated LLM error")

def test_agent_simulation():
    """Simulate agent processing times."""
    print("👥 Testing agent simulation...")
    
    # Simulate different agents with varying processing times
    agent_scenarios = [
        ('agent_emi_agent', 'msg_001', 3.0),
        ('agent_emi_agent', 'msg_002', 15.0),  # Slow agent
        ('agent_emi_audio_agent', 'msg_003', 2.0),
        ('agent_emi_result_handler', 'msg_004', 8.0),
    ]
    
    for agent, msg_id, duration in agent_scenarios:
        timer_id = performance_monitor.start_timer(agent, msg_id)
        time.sleep(duration)
        performance_monitor.end_timer(timer_id, {
            'status': 'success',
            'agent_name': agent.replace('agent_', ''),
            'message_id': msg_id
        })
        print(f"   ✅ Simulated {agent}: {duration:.1f}s")

def test_rag_simulation():
    """Simulate RAG retrieval times."""
    print("🔍 Testing RAG simulation...")
    
    # Simulate RAG operations
    rag_scenarios = [
        ('rag_retrieval_emi_agent', '3_scopes', 0.8),
        ('rag_retrieval_emi_agent', '5_scopes', 1.2),
        ('rag_retrieval_emi_audio_agent', '2_scopes', 0.5),
    ]
    
    for operation, request_id, duration in rag_scenarios:
        timer_id = performance_monitor.start_timer(operation, request_id)
        time.sleep(duration)
        performance_monitor.end_timer(timer_id, {
            'status': 'success',
            'agent_name': operation.split('_')[2],
            'query_length': 50,
            'scopes_count': int(request_id.split('_')[0]),
            'results_count': 2
        })
        print(f"   ✅ Simulated {operation}: {duration:.1f}s")

def run_analysis():
    """Run performance analysis on the test data."""
    print("\n📊 Running performance analysis...")
    
    # General analysis
    print("\n--- General Performance Analysis ---")
    analyzer = PerformanceAnalyzer()
    analyzer.print_analysis()
    
    # LLM-specific analysis
    print("\n--- LLM Performance Analysis ---")
    llm_analyzer = LLMPerformanceAnalyzer()
    llm_analyzer.print_llm_analysis()

def main():
    """Main test function."""
    print("🚀 Starting Performance Monitoring Tests")
    print("=" * 50)
    
    try:
        test_basic_timing()
        test_llm_simulation()
        test_agent_simulation()
        test_rag_simulation()
        
        run_analysis()
        
        print("\n✅ All tests completed successfully!")
        print("\n📁 Test data has been collected and can be analyzed.")
        print("💡 Run the analysis scripts to see detailed performance metrics.")
        
    except Exception as e:
        print(f"\n❌ Test failed with error: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    main()
