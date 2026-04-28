#!/usr/bin/env python3
"""
Performance Analyzer for Emi System
Analyzes performance metrics and provides optimization recommendations.
"""

import json
from typing import Dict, List
from datetime import datetime

from app.assistant.performance.performance_monitor import performance_monitor
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)

class PerformanceAnalyzer:
    """Analyzes performance metrics and provides optimization recommendations."""
    
    def __init__(self):
        self.monitor = performance_monitor
        
    def analyze_system_performance(self) -> Dict:
        """Comprehensive performance analysis."""
        stats = self.monitor.get_all_stats()
        
        analysis = {
            'timestamp': datetime.now().isoformat(),
            'overview': self._generate_overview(stats),
            'bottlenecks': self._identify_bottlenecks(stats),
            'recommendations': self._generate_recommendations(stats),
            'slow_operations': self.monitor.get_slow_operations(threshold=1.0),
            'detailed_stats': stats
        }
        
        return analysis
    
    def _generate_overview(self, stats: Dict) -> Dict:
        """Generate a high-level overview of system performance."""
        total_operations = sum(stat.get('count', 0) for stat in stats.values())
        avg_durations = [stat.get('avg_duration', 0) for stat in stats.values() if stat.get('count', 0) > 0]
        
        return {
            'total_operations': total_operations,
            'unique_operations': len(stats),
            'overall_avg_duration': sum(avg_durations) / len(avg_durations) if avg_durations else 0,
            'slowest_operation': max(stats.items(), key=lambda x: x[1].get('avg_duration', 0)) if stats else None,
            'fastest_operation': min(stats.items(), key=lambda x: x[1].get('avg_duration', 0)) if stats else None
        }
    
    def _identify_bottlenecks(self, stats: Dict) -> List[Dict]:
        """Identify performance bottlenecks."""
        bottlenecks = []
        
        for operation, stat in stats.items():
            if stat.get('count', 0) < 3:  # Skip operations with too few samples
                continue
                
            avg_duration = stat.get('avg_duration', 0)
            recent_avg = stat.get('recent_avg', 0)
            
            # Check for slow operations
            if avg_duration > 2.0:
                bottlenecks.append({
                    'operation': operation,
                    'type': 'slow_operation',
                    'severity': 'high' if avg_duration > 5.0 else 'medium',
                    'avg_duration': avg_duration,
                    'description': f"Operation '{operation}' is consistently slow ({avg_duration:.2f}s average)"
                })
            
            # Check for degrading performance
            if recent_avg > avg_duration * 1.5 and recent_avg > 1.0:
                bottlenecks.append({
                    'operation': operation,
                    'type': 'degrading_performance',
                    'severity': 'medium',
                    'avg_duration': avg_duration,
                    'recent_avg': recent_avg,
                    'description': f"Operation '{operation}' performance is degrading (recent avg: {recent_avg:.2f}s vs overall: {avg_duration:.2f}s)"
                })
        
        return sorted(bottlenecks, key=lambda x: x.get('avg_duration', 0), reverse=True)
    
    def _generate_recommendations(self, stats: Dict) -> List[Dict]:
        """Generate optimization recommendations."""
        recommendations = []
        
        # Check for LLM-related bottlenecks
        llm_operations = [op for op in stats.keys() if 'llm' in op.lower() or 'openai' in op.lower()]
        for op in llm_operations:
            avg_duration = stats[op].get('avg_duration', 0)
            if avg_duration > 3.0:
                recommendations.append({
                    'category': 'LLM Optimization',
                    'priority': 'high' if avg_duration > 5.0 else 'medium',
                    'recommendation': f"Consider reducing LLM timeout or using a faster model for '{op}'",
                    'expected_improvement': f"Reduce latency by {min(avg_duration - 2.0, avg_duration * 0.3):.1f}s"
                })
        
        # Check for RAG-related bottlenecks
        rag_operations = [op for op in stats.keys() if 'rag' in op.lower() or 'embedding' in op.lower()]
        for op in rag_operations:
            avg_duration = stats[op].get('avg_duration', 0)
            if avg_duration > 1.0:
                recommendations.append({
                    'category': 'RAG Optimization',
                    'priority': 'medium',
                    'recommendation': f"Optimize RAG retrieval for '{op}' by reducing search scope or improving caching",
                    'expected_improvement': f"Reduce latency by {avg_duration * 0.5:.1f}s"
                })
        
        # Check for event processing bottlenecks
        event_operations = [op for op in stats.keys() if 'event' in op.lower() or 'queue' in op.lower()]
        for op in event_operations:
            avg_duration = stats[op].get('avg_duration', 0)
            if avg_duration > 0.5:
                recommendations.append({
                    'category': 'Event Processing',
                    'priority': 'medium',
                    'recommendation': f"Optimize event processing for '{op}' by reducing sleep intervals",
                    'expected_improvement': f"Reduce latency by {avg_duration * 0.3:.1f}s"
                })
        
        # General recommendations
        if len(stats) > 10:
            recommendations.append({
                'category': 'System Architecture',
                'priority': 'low',
                'recommendation': "Consider implementing request batching for similar operations",
                'expected_improvement': "Reduce overall system load by 10-20%"
            })
        
        return recommendations
    
    def print_analysis(self):
        """Print a formatted performance analysis."""
        analysis = self.analyze_system_performance()
        
        print("\n" + "="*60)
        print("EMI SYSTEM PERFORMANCE ANALYSIS")
        print("="*60)
        
        # Overview
        overview = analysis['overview']
        print(f"\n📊 OVERVIEW:")
        print(f"   Total Operations: {overview['total_operations']}")
        print(f"   Unique Operations: {overview['unique_operations']}")
        print(f"   Overall Avg Duration: {overview['overall_avg_duration']:.3f}s")
        
        if overview['slowest_operation']:
            slowest_op, slowest_stat = overview['slowest_operation']
            print(f"   Slowest Operation: {slowest_op} ({slowest_stat['avg_duration']:.3f}s)")
        
        # Bottlenecks
        if analysis['bottlenecks']:
            print(f"\n🚨 BOTTLENECKS ({len(analysis['bottlenecks'])} found):")
            for i, bottleneck in enumerate(analysis['bottlenecks'][:5], 1):
                severity_icon = "🔴" if bottleneck['severity'] == 'high' else "🟡"
                print(f"   {i}. {severity_icon} {bottleneck['description']}")
        
        # Recommendations
        if analysis['recommendations']:
            print(f"\n💡 RECOMMENDATIONS ({len(analysis['recommendations'])}):")
            for i, rec in enumerate(analysis['recommendations'][:5], 1):
                priority_icon = "🔴" if rec['priority'] == 'high' else "🟡" if rec['priority'] == 'medium' else "🟢"
                print(f"   {i}. {priority_icon} [{rec['category']}] {rec['recommendation']}")
                print(f"      Expected improvement: {rec['expected_improvement']}")
        
        # Slow operations
        slow_ops = analysis['slow_operations']
        if slow_ops:
            print(f"\n🐌 RECENT SLOW OPERATIONS ({len(slow_ops)}):")
            for i, op in enumerate(slow_ops[:5], 1):
                print(f"   {i}. {op['operation']} - {op['duration']:.2f}s")
        
        print("\n" + "="*60)
    
    def export_analysis(self, filepath: str):
        """Export detailed analysis to JSON file."""
        analysis = self.analyze_system_performance()
        with open(filepath, 'w') as f:
            json.dump(analysis, f, indent=2)
        logger.info(f"Performance analysis exported to {filepath}")

def main():
    """Main function to run performance analysis."""
    analyzer = PerformanceAnalyzer()
    analyzer.print_analysis()
    
    # Export analysis
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    analyzer.export_analysis(f"performance_analysis_{timestamp}.json")

if __name__ == "__main__":
    main()
