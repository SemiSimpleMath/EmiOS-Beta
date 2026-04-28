from datetime import datetime, timezone, date
import json
import os
from pathlib import Path
from app.assistant.utils.logging_config import get_maintenance_logger

logger = get_maintenance_logger(__name__)

class DailySummaryStorage:
    """
    Handles storage and retrieval of daily summary data.
    Saves summaries to JSON files organized by date.
    """
    
    def __init__(self, storage_dir=None):
        if storage_dir is None:
            # Default to app/daily_summaries for Flask app compatibility
            from pathlib import Path
            app_root = Path(__file__).parent.parent.parent  # Go up to app directory
            storage_dir = app_root / "daily_summaries"
        
        self.storage_dir = Path(storage_dir)
        self.storage_dir.mkdir(exist_ok=True)
        logger.info(f"Daily summary storage initialized at: {self.storage_dir.absolute()}")
    
    def save_daily_summary(self, summary_data, date_str=None):
        """
        Save daily summary data to a JSON file.
        
        Args:
            summary_data (dict): The daily summary data to save
            date_str (str, optional): Date string in YYYY-MM-DD format. 
                                    If None, uses today's date.
        
        Returns:
            str: Path to the saved file
        """
        try:
            # Use provided date or today's date
            if date_str is None:
                date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            
            # Create filename
            filename = f"daily_summary_{date_str}.json"
            filepath = self.storage_dir / filename
            
            # Add metadata
            summary_with_metadata = {
                "metadata": {
                    "generated_at": datetime.now(timezone.utc).isoformat(),
                    "date": date_str,
                    "version": "1.0"
                },
                "summary": summary_data
            }
            
            # Save to file
            with open(filepath, 'w', encoding='utf-8') as f:
                json.dump(summary_with_metadata, f, indent=2, ensure_ascii=False)
            
            logger.info(f"✅ Daily summary saved to: {filepath}")
            return str(filepath)
            
        except Exception as e:
            logger.error(f"❌ Error saving daily summary: {e}")
            raise
    
    def get_daily_summary(self, date_str=None):
        """
        Retrieve daily summary data for a specific date.
        
        Args:
            date_str (str, optional): Date string in YYYY-MM-DD format.
                                    If None, uses today's date.
        
        Returns:
            dict: The daily summary data, or None if not found
        """
        try:
            # Use provided date or today's date
            if date_str is None:
                date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            
            # Create filename
            filename = f"daily_summary_{date_str}.json"
            filepath = self.storage_dir / filename
            
            if not filepath.exists():
                logger.info(f"No daily summary found for date: {date_str}")
                return None
            
            # Load from file
            with open(filepath, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            logger.info(f"📖 Daily summary loaded for date: {date_str}")
            return data
            
        except Exception as e:
            logger.error(f"❌ Error loading daily summary: {e}")
            return None
    
    def get_latest_daily_summary(self):
        """
        Get the most recent daily summary.
        
        Returns:
            dict: The most recent daily summary data, or None if none found
        """
        try:
            # Find all daily summary files
            summary_files = list(self.storage_dir.glob("daily_summary_*.json"))
            
            if not summary_files:
                logger.info("No daily summary files found")
                return None
            
            # Sort by modification time (newest first)
            summary_files.sort(key=lambda x: x.stat().st_mtime, reverse=True)
            
            # Load the most recent
            latest_file = summary_files[0]
            with open(latest_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            logger.info(f"📖 Latest daily summary loaded from: {latest_file.name}")
            return data
            
        except Exception as e:
            logger.error(f"❌ Error loading latest daily summary: {e}")
            return None
    
    def list_available_summaries(self):
        """
        List all available daily summaries.
        
        Returns:
            list: List of available summary dates
        """
        try:
            summary_files = list(self.storage_dir.glob("daily_summary_*.json"))
            dates = []
            
            for file in summary_files:
                # Extract date from filename
                filename = file.name
                if filename.startswith("daily_summary_") and filename.endswith(".json"):
                    date_part = filename[14:-5]  # Remove "daily_summary_" and ".json"
                    dates.append(date_part)
            
            dates.sort(reverse=True)  # Newest first
            return dates
            
        except Exception as e:
            logger.error(f"❌ Error listing available summaries: {e}")
            return []
    
    def delete_daily_summary(self, date_str):
        """
        Delete a daily summary for a specific date.
        
        Args:
            date_str (str): Date string in YYYY-MM-DD format
        
        Returns:
            bool: True if deleted, False if not found or error
        """
        try:
            filename = f"daily_summary_{date_str}.json"
            filepath = self.storage_dir / filename
            
            if filepath.exists():
                filepath.unlink()
                logger.info(f"🗑️ Daily summary deleted for date: {date_str}")
                return True
            else:
                logger.info(f"No daily summary found to delete for date: {date_str}")
                return False
                
        except Exception as e:
            logger.error(f"❌ Error deleting daily summary: {e}")
            return False
    
    def get_summary_stats(self):
        """
        Get statistics about stored daily summaries.
        
        Returns:
            dict: Statistics about stored summaries
        """
        try:
            summary_files = list(self.storage_dir.glob("daily_summary_*.json"))
            
            if not summary_files:
                return {
                    "total_summaries": 0,
                    "oldest_date": None,
                    "newest_date": None,
                    "storage_size_mb": 0
                }
            
            # Get dates
            dates = []
            total_size = 0
            
            for file in summary_files:
                filename = file.name
                if filename.startswith("daily_summary_") and filename.endswith(".json"):
                    date_part = filename[14:-5]
                    dates.append(date_part)
                    total_size += file.stat().st_size
            
            dates.sort()
            
            return {
                "total_summaries": len(dates),
                "oldest_date": dates[0] if dates else None,
                "newest_date": dates[-1] if dates else None,
                "storage_size_mb": round(total_size / (1024 * 1024), 2)
            }
            
        except Exception as e:
            logger.error(f"❌ Error getting summary stats: {e}")
            return {"error": str(e)}
