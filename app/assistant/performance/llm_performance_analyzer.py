#!/usr/bin/env python3
"""
LLM Performance Analyzer for Emi System
Specialized analysis for LLM-related performance issues.
"""

import json
from typing import Dict, List
from datetime import datetime

from app.assistant.performance.performance_monitor import performance_monitor
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)

class LLMPerformanceAnalyzer:
    """Specialized analyzer for LLM performance issues."""
    
    def __init__(self):
        self.monitor = performance_monitor
        
    def analyze_llm_performance(self) -> Dict:
        """Analyze LLM-specific performance metrics."""
        stats = self.monitor.get_all_stats()
        
        # Filter for LLM-related operations
        llm_operations = {
            k: v for k, v in stats.items() 
            if any(keyword in k.lower() for keyword in ['llm', 'openai', 'gpt', 'structured_output'])
        }
        
        agent_operations = {
            k: v for k, v in stats.items() 
            if k.startswith('agent_')
        }
        
        rag_operations = {
            k: v for k, v in stats.items() 
            if k.startswith('rag_')
        }
        
        analysis = {
            'timestamp': datetime.now().isoformat(),
            'llm_operations': self._analyze_llm_operations(llm_operations),
            'agent_performance': self._analyze_agent_performance(agent_operations),
            'rag_performance': self._analyze_rag_performance(rag_operations),
            'slow_llm_calls': self._get_slow_llm_calls(),
            'recommendations': self._generate_llm_recommendations(llm_operations, agent_operations)
        }
        
        return analysis
    
    def _analyze_llm_operations(self, llm_stats: Dict) -> Dict:
        """Analyze LLM operation performance."""
        if not llm_stats:
            return {'message': 'No LLM operations found'}
        
        analysis = {
            'total_calls': sum(stat.get('count', 0) for stat in llm_stats.values()),
            'avg_duration': sum(stat.get('avg_duration', 0) * stat.get('count', 0) for stat in llm_stats.values()) / 
                          sum(stat.get('count', 0) for stat in llm_stats.values()) if any(stat.get('count', 0) for stat in llm_stats.values()) else 0,
            'slowest_operation': max(llm_stats.items(), key=lambda x: x[1].get('avg_duration', 0)) if llm_stats else None,
            'operation_breakdown': {}
        }
        
        for operation, stat in llm_stats.items():
            analysis['operation_breakdown'][operation] = {
                'count': stat.get('count', 0),
                'avg_duration': stat.get('avg_duration', 0),
                'max_duration': stat.get('max_duration', 0),
                'recent_avg': stat.get('recent_avg', 0)
            }
        
        return analysis
    
    def _analyze_agent_performance(self, agent_stats: Dict) -> Dict:
        """Analyze agent performance with focus on LLM usage."""
        if not agent_stats:
            return {'message': 'No agent operations found'}
        
        analysis = {
            'total_agents': len(agent_stats),
            'slowest_agent': max(agent_stats.items(), key=lambda x: x[1].get('avg_duration', 0)) if agent_stats else None,
            'agent_breakdown': {}
        }
        
        for agent, stat in agent_stats.items():
            agent_name = agent.replace('agent_', '')
            analysis['agent_breakdown'][agent_name] = {
                'count': stat.get('count', 0),
                'avg_duration': stat.get('avg_duration', 0),
                'max_duration': stat.get('max_duration', 0),
                'recent_avg': stat.get('recent_avg', 0)
            }
        
        return analysis
    
    def _analyze_rag_performance(self, rag_stats: Dict) -> Dict:
        """Analyze RAG performance."""
        if not rag_stats:
            return {'message': 'No RAG operations found'}
        
        analysis = {
            'total_queries': sum(stat.get('count', 0) for stat in rag_stats.values()),
            'avg_duration': sum(stat.get('avg_duration', 0) * stat.get('count', 0) for stat in rag_stats.values()) / 
                          sum(stat.get('count', 0) for stat in rag_stats.values()) if any(stat.get('count', 0) for stat in rag_stats.values()) else 0,
            'slowest_rag': max(rag_stats.items(), key=lambda x: x[1].get('avg_duration', 0)) if rag_stats else None
        }
        
        return analysis
    
    def _get_slow_llm_calls(self, threshold: float = 10.0) -> List[Dict]:
        """Get LLM calls that took longer than threshold."""
        slow_calls = []
        
        for operation, metrics in self.monitor.metrics.items():
            if any(keyword in operation.lower() for keyword in ['llm', 'openai', 'gpt', 'structured_output']):
                for metric in metrics:
                    if metric['duration'] > threshold:
                        slow_calls.append({
                            'operation': operation,
                            'duration': metric['duration'],
                            'timestamp': metric['timestamp'],
                            'request_id': metric['request_id'],
                            'additional_data': metric['additional_data']
                        })
        
        return sorted(slow_calls, key=lambda x: x['duration'], reverse=True)
    
    def _generate_llm_recommendations(self, llm_stats: Dict, agent_stats: Dict) -> List[Dict]:
        """Generate LLM-specific recommendations."""
        recommendations = []
        
        # Check for slow LLM operations
        for operation, stat in llm_stats.items():
            avg_duration = stat.get('avg_duration', 0)
            if avg_duration > 15.0:
                recommendations.append({
                    'category': 'LLM Performance',
                    'priority': 'high',
                    'operation': operation,
                    'issue': f"LLM operation '{operation}' is consistently slow ({avg_duration:.1f}s average)",
                    'recommendation': "Consider optimizing prompt size, using a faster model, or implementing caching",
                    'expected_improvement': f"Reduce latency by {avg_duration * 0.3:.1f}s"
                })
        
        # Check for agents with large prompts
        for agent, stat in agent_stats.items():
            avg_duration = stat.get('avg_duration', 0)
            if avg_duration > 20.0:
                agent_name = agent.replace('agent_', '')
                recommendations.append({
                    'category': 'Agent Optimization',
                    'priority': 'medium',
                    'operation': agent_name,
                    'issue': f"Agent '{agent_name}' has long processing time ({avg_duration:.1f}s average)",
                    'recommendation': "Review prompt size and RAG context injection for this agent",
                    'expected_improvement': f"Reduce latency by {avg_duration * 0.2:.1f}s"
                })
        
        return recommendations
    
    def print_llm_analysis(self):
        """Print a formatted LLM performance analysis."""
        analysis = self.analyze_llm_performance()
        
        print("\n" + "="*60)
        print("LLM PERFORMANCE ANALYSIS")
        print("="*60)
        
        # LLM Operations Overview
        llm_ops = analysis['llm_operations']
        if 'message' not in llm_ops:
            print(f"\n🤖 LLM OPERATIONS:")
            print(f"   Total Calls: {llm_ops['total_calls']}")
            print(f"   Average Duration: {llm_ops['avg_duration']:.2f}s")
            
            if llm_ops['slowest_operation']:
                slowest_op, slowest_stat = llm_ops['slowest_operation']
                print(f"   Slowest Operation: {slowest_op} ({slowest_stat['avg_duration']:.2f}s)")
        
        # Agent Performance
        agent_perf = analysis['agent_performance']
        if 'message' not in agent_perf:
            print(f"\n👥 AGENT PERFORMANCE:")
            print(f"   Total Agents: {agent_perf['total_agents']}")
            
            if agent_perf['slowest_agent']:
                slowest_agent, slowest_stat = agent_perf['slowest_agent']
                agent_name = slowest_agent.replace('agent_', '')
                print(f"   Slowest Agent: {agent_name} ({slowest_stat['avg_duration']:.2f}s)")
        
        # RAG Performance
        rag_perf = analysis['rag_performance']
        if 'message' not in rag_perf:
            print(f"\n🔍 RAG PERFORMANCE:")
            print(f"   Total Queries: {rag_perf['total_queries']}")
            print(f"   Average Duration: {rag_perf['avg_duration']:.2f}s")
        
        # Slow LLM Calls
        slow_calls = analysis['slow_llm_calls']
        if slow_calls:
            print(f"\n🐌 SLOW LLM CALLS ({len(slow_calls)}):")
            for i, call in enumerate(slow_calls[:5], 1):
                print(f"   {i}. {call['operation']} - {call['duration']:.1f}s")
                if call['additional_data'].get('model'):
                    print(f"      Model: {call['additional_data']['model']}")
                if call['additional_data'].get('message_count'):
                    print(f"      Messages: {call['additional_data']['message_count']}")
        
        # Recommendations
        recommendations = analysis['recommendations']
        if recommendations:
            print(f"\n💡 LLM RECOMMENDATIONS ({len(recommendations)}):")
            for i, rec in enumerate(recommendations[:5], 1):
                priority_icon = "🔴" if rec['priority'] == 'high' else "🟡"
                print(f"   {i}. {priority_icon} [{rec['category']}] {rec['issue']}")
                print(f"      Recommendation: {rec['recommendation']}")
                print(f"      Expected improvement: {rec['expected_improvement']}")
        
        print("\n" + "="*60)
    
    def export_llm_analysis(self, filepath: str):
        """Export LLM analysis to JSON file."""
        analysis = self.analyze_llm_performance()
        with open(filepath, 'w') as f:
            json.dump(analysis, f, indent=2)
        logger.info(f"LLM performance analysis exported to {filepath}")

def main():
    """Main function to run LLM performance analysis."""
    analyzer = LLMPerformanceAnalyzer()
    analyzer.print_llm_analysis()
    
    # Export analysis
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    analyzer.export_llm_analysis(f"llm_performance_analysis_{timestamp}.json")

if __name__ == "__main__":
    main()
