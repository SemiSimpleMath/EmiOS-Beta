import time
import threading
import uuid
from typing import Dict, List, Optional
from datetime import datetime, timezone
from collections import defaultdict, deque
import json

from app.assistant.utils.logging_config import get_logger
logger = get_logger(__name__)

class PerformanceMonitor:
    """Monitors and tracks performance metrics across the Emi system."""
    
    def __init__(self, max_history: int = 1000):
        self.max_history = max_history
        self.metrics = defaultdict(lambda: deque(maxlen=max_history))
        self.active_timers = {}
        self.lock = threading.Lock()
        
    def start_timer(self, operation_name: str, request_id: Optional[str] = None) -> str:
        """Start timing an operation."""
        timer_id = f"{operation_name}_{request_id or int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}"
        with self.lock:
            self.active_timers[timer_id] = {
                'operation': operation_name,
                'start_time': time.time(),
                'request_id': request_id
            }
        return timer_id
    
    def end_timer(self, timer_id: str, additional_data: Optional[Dict] = None) -> Optional[float]:
        """End timing an operation and record the duration."""
        with self.lock:
            if timer_id not in self.active_timers:
                logger.warning(f"Timer {timer_id} not found")
                return None
                
            timer_data = self.active_timers.pop(timer_id)
            duration = time.time() - timer_data['start_time']
            
            metric_data = {
                'duration': duration,
                'timestamp': datetime.now(timezone.utc),
                'request_id': timer_data['request_id'],
                'additional_data': additional_data or {}
            }
            
            self.metrics[timer_data['operation']].append(metric_data)
            
            # Log slow operations
            if duration > 5.0:  # Log operations taking more than 5 seconds
                logger.warning(f"Slow operation detected: {timer_data['operation']} took {duration:.2f}s")
            
            return duration
    
    def get_operation_stats(self, operation_name: str) -> Dict:
        """Get statistics for a specific operation."""
        with self.lock:
            if operation_name not in self.metrics:
                return {}
            
            durations = [m['duration'] for m in self.metrics[operation_name]]
            if not durations:
                return {}
            
            return {
                'count': len(durations),
                'avg_duration': sum(durations) / len(durations),
                'min_duration': min(durations),
                'max_duration': max(durations),
                'recent_avg': sum(durations[-10:]) / min(10, len(durations)) if durations else 0
            }
    
    def get_all_stats(self) -> Dict:
        """Get statistics for all operations."""
        with self.lock:
            stats = {}
            for operation in self.metrics:
                stats[operation] = self.get_operation_stats(operation)
            return stats
    
    def get_slow_operations(self, threshold: float = 2.0) -> List[Dict]:
        """Get recent operations that took longer than the threshold."""
        with self.lock:
            slow_ops = []
            for operation, metrics in self.metrics.items():
                for metric in metrics:
                    if metric['duration'] > threshold:
                        slow_ops.append({
                            'operation': operation,
                            'duration': metric['duration'],
                            'timestamp': metric['timestamp'],
                            'request_id': metric['request_id']
                        })
            return sorted(slow_ops, key=lambda x: x['duration'], reverse=True)
    
    def export_metrics(self, filepath: str):
        """Export metrics to a JSON file."""
        with self.lock:
            export_data = {
                'export_timestamp': datetime.now(timezone.utc).isoformat(),
                'metrics': {}
            }
            
            for operation, metrics in self.metrics.items():
                export_data['metrics'][operation] = [
                    {
                        'duration': m['duration'],
                        'timestamp': m['timestamp'].isoformat(),
                        'request_id': m['request_id'],
                        'additional_data': m['additional_data']
                    }
                    for m in metrics
                ]
            
            with open(filepath, 'w') as f:
                json.dump(export_data, f, indent=2)
            
            logger.info(f"Performance metrics exported to {filepath}")

# Global instance
performance_monitor = PerformanceMonitor()

def monitor_operation(operation_name: str, request_id: Optional[str] = None):
    """Decorator to monitor operation performance."""
    def decorator(func):
        def wrapper(*args, **kwargs):
            timer_id = performance_monitor.start_timer(operation_name, request_id)
            try:
                result = func(*args, **kwargs)
                performance_monitor.end_timer(timer_id, {'status': 'success'})
                return result
            except Exception as e:
                performance_monitor.end_timer(timer_id, {'status': 'error', 'error': str(e)})
                raise
        return wrapper
    return decorator
