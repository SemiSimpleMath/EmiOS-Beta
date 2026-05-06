"""
Taxonomy Maintenance Agent

This agent performs breadth-first search (BFS) traversal of the taxonomy tree,
analyzing each branch to:
1. Check if all categories have descriptions
2. Verify that child categories belong to their parent
3. Suggest improvements and reorganizations

Usage:
    python app/assistant/kg_core/taxonomy/taxonomy_maintenance_agent.py
"""

from app.assistant.utils.logging_config import get_logger
from typing import Dict, Any, List, Optional, Tuple
from collections import deque
from sqlalchemy.orm import Session
from app.models.base import get_session
from app.assistant.kg_core.taxonomy.models import Taxonomy
from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.utils.pydantic_classes import Message
logger = get_logger(__name__)


class TaxonomyMaintenanceAgent:
    """
    Agent that performs BFS traversal of taxonomy tree and analyzes each branch
    """
    
    def __init__(self, session: Session):
        self.session = session
        # We'll create a simple LLM-based analyzer
        # For now, we'll use a direct LLM call, but this could be a dedicated agent
        
    def get_root_categories(self) -> List[Taxonomy]:
        """Get all root-level taxonomy categories"""
        return self.session.query(Taxonomy).filter(
            Taxonomy.parent_id == None
        ).order_by(Taxonomy.label).all()
    
    def get_children(self, taxonomy_id: int) -> List[Taxonomy]:
        """Get all direct children of a taxonomy category"""
        return self.session.query(Taxonomy).filter(
            Taxonomy.parent_id == taxonomy_id
        ).order_by(Taxonomy.label).all()
    
    def get_full_path(self, taxonomy: Taxonomy) -> str:
        """Get the full path of a taxonomy category"""
        path_parts = []
        current = taxonomy
        
        while current:
            path_parts.insert(0, current.label)
            if current.parent_id:
                current = self.session.query(Taxonomy).filter(
                    Taxonomy.id == current.parent_id
                ).first()
            else:
                current = None
        
        return " > ".join(path_parts)
    
    def analyze_branch(self, taxonomy: Taxonomy) -> Dict[str, Any]:
        """
        Analyze a single taxonomy branch
        
        Returns:
            Dict with analysis results including:
            - missing_descriptions: List of categories without descriptions
            - misplaced_children: List of children that might not belong
            - suggestions: List of improvement suggestions
        """
        full_path = self.get_full_path(taxonomy)
        children = self.get_children(taxonomy.id)
        
        logger.info(f"\n{'='*70}")
        logger.info(f"📊 Analyzing: {full_path}")
        logger.info(f"   ID: {taxonomy.id}")
        logger.info(f"   Description: {taxonomy.description or '❌ MISSING'}")
        logger.info(f"   Children: {len(children)}")
        logger.info(f"{'='*70}")
        
        # Check for missing description
        missing_description = not taxonomy.description or taxonomy.description.strip() == ""
        
        # Prepare analysis
        analysis = {
            "taxonomy_id": taxonomy.id,
            "label": taxonomy.label,
            "full_path": full_path,
            "description": taxonomy.description,
            "missing_description": missing_description,
            "children_count": len(children),
            "children": [],
            "issues": [],
            "suggestions": []
        }
        
        # Check for missing description
        if missing_description:
            analysis["issues"].append({
                "type": "missing_description",
                "severity": "high",
                "message": f"Category '{taxonomy.label}' has no description"
            })
            analysis["suggestions"].append({
                "type": "add_description",
                "category": taxonomy.label,
                "suggestion": f"Add a clear description for '{taxonomy.label}' explaining what types of entities belong here"
            })
        
        # Analyze children
        if children:
            child_labels = [child.label for child in children]
            child_descriptions = {
                child.label: child.description or "No description"
                for child in children
            }
            
            # Build context for LLM analysis
            children_summary = []
            for child in children:
                child_info = {
                    "label": child.label,
                    "description": child.description or "❌ No description",
                    "has_description": bool(child.description and child.description.strip())
                }
                children_summary.append(child_info)
                analysis["children"].append(child_info)
                
                # Check for missing child descriptions
                if not child_info["has_description"]:
                    analysis["issues"].append({
                        "type": "missing_child_description",
                        "severity": "medium",
                        "message": f"Child category '{child.label}' has no description"
                    })
            
            # Use LLM to analyze if children belong to parent
            logger.info(f"\n🤖 Asking LLM to analyze children of '{taxonomy.label}'...")
            llm_analysis = self._llm_analyze_children(
                parent_label=taxonomy.label,
                parent_description=taxonomy.description or "No description provided",
                parent_path=full_path,
                children_info=children_summary
            )
            
            if llm_analysis:
                analysis["llm_analysis"] = llm_analysis
                
                # Extract misplaced children from LLM response
                if "misplaced_children" in llm_analysis:
                    for misplaced in llm_analysis["misplaced_children"]:
                        analysis["issues"].append({
                            "type": "misplaced_child",
                            "severity": "high",
                            "message": f"Child '{misplaced['label']}' may not belong under '{taxonomy.label}'",
                            "reason": misplaced.get("reason", "Unknown")
                        })
                        analysis["suggestions"].append({
                            "type": "move_category",
                            "category": misplaced["label"],
                            "current_parent": taxonomy.label,
                            "suggested_parent": misplaced.get("suggested_parent", "Unknown"),
                            "reason": misplaced.get("reason", "Unknown")
                        })
                
                # Extract general suggestions from LLM
                if "suggestions" in llm_analysis:
                    for suggestion in llm_analysis["suggestions"]:
                        analysis["suggestions"].append({
                            "type": "general",
                            "suggestion": suggestion
                        })
        
        return analysis
    
    def _llm_analyze_children(
        self,
        parent_label: str,
        parent_description: str,
        parent_path: str,
        children_info: List[Dict[str, Any]]
    ) -> Optional[Dict[str, Any]]:
        """
        Use LLM to analyze if children belong to parent category
        """
        try:
            # Create agent for taxonomy analysis
            agent = DI.agent_factory.create_agent("knowledge_graph_add::taxonomy_critic")
            
            # Build prompt
            children_text = "\n".join([
                f"- {child['label']}: {child['description']}"
                for child in children_info
            ])
            
            prompt = f"""
Analyze this taxonomy branch for correctness and coherence:

**Parent Category:** {parent_label}
**Full Path:** {parent_path}
**Parent Description:** {parent_description}

**Child Categories:**
{children_text}

Please analyze:
1. Do all child categories logically belong under "{parent_label}"?
2. Are there any children that seem misplaced or would fit better elsewhere?
3. Are there any organizational issues or improvements you'd suggest?

Respond in JSON format:
{{
    "misplaced_children": [
        {{
            "label": "child label",
            "reason": "why it doesn't belong",
            "suggested_parent": "where it should go"
        }}
    ],
    "suggestions": [
        "general improvement suggestion 1",
        "general improvement suggestion 2"
    ]
}}
"""
            
            message = Message(agent_input={"analysis_prompt": prompt})
            result = agent.action_handler(message)
            
            if result and hasattr(result, 'data') and result.data:
                return result.data
            
            return None
            
        except Exception as e:
            logger.error(f"Error in LLM analysis: {e}")
            return None
    
    def bfs_traverse_and_analyze(
        self,
        start_taxonomy_id: Optional[int] = None,
        max_depth: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        """
        Perform BFS traversal of taxonomy tree and analyze each branch
        
        Args:
            start_taxonomy_id: Start from this taxonomy (None = start from all roots)
            max_depth: Maximum depth to traverse (None = unlimited)
        
        Returns:
            List of analysis results for each taxonomy category
        """
        results = []
        
        # Initialize queue with starting taxonomies
        if start_taxonomy_id:
            start_taxonomy = self.session.query(Taxonomy).filter(
                Taxonomy.id == start_taxonomy_id
            ).first()
            if not start_taxonomy:
                logger.error(f"Taxonomy with ID {start_taxonomy_id} not found")
                return results
            queue = deque([(start_taxonomy, 0)])  # (taxonomy, depth)
        else:
            # Start from all root categories
            roots = self.get_root_categories()
            queue = deque([(root, 0) for root in roots])
        
        logger.info(f"\n🚀 Starting BFS traversal...")
        logger.info(f"   Starting from: {len(queue)} root categories")
        logger.info(f"   Max depth: {max_depth or 'unlimited'}")
        
        visited = set()
        
        while queue:
            taxonomy, depth = queue.popleft()
            
            # Skip if already visited (prevent cycles)
            if taxonomy.id in visited:
                continue
            visited.add(taxonomy.id)
            
            # Skip if max depth exceeded
            if max_depth is not None and depth > max_depth:
                continue
            
            # Analyze this branch
            analysis = self.analyze_branch(taxonomy)
            analysis["depth"] = depth
            results.append(analysis)
            
            # Print summary
            issue_count = len(analysis["issues"])
            suggestion_count = len(analysis["suggestions"])
            if issue_count > 0 or suggestion_count > 0:
                logger.info(f"   ⚠️  Found {issue_count} issues, {suggestion_count} suggestions")
            else:
                logger.info(f"   ✅ No issues found")
            
            # Add children to queue
            children = self.get_children(taxonomy.id)
            for child in children:
                queue.append((child, depth + 1))
        
        logger.info(f"\n✅ BFS traversal complete!")
        logger.info(f"   Analyzed {len(results)} categories")
        
        return results
    
    def generate_report(self, results: List[Dict[str, Any]]) -> str:
        """
        Generate a human-readable report from analysis results
        """
        report = []
        report.append("=" * 80)
        report.append("TAXONOMY MAINTENANCE REPORT")
        report.append("=" * 80)
        report.append("")
        
        # Summary statistics
        total_categories = len(results)
        categories_with_issues = sum(1 for r in results if r["issues"])
        total_issues = sum(len(r["issues"]) for r in results)
        total_suggestions = sum(len(r["suggestions"]) for r in results)
        missing_descriptions = sum(1 for r in results if r["missing_description"])
        
        report.append(f"📊 SUMMARY")
        report.append(f"   Total categories analyzed: {total_categories}")
        report.append(f"   Categories with issues: {categories_with_issues}")
        report.append(f"   Total issues found: {total_issues}")
        report.append(f"   Total suggestions: {total_suggestions}")
        report.append(f"   Missing descriptions: {missing_descriptions}")
        report.append("")
        
        # Categories with issues
        if categories_with_issues > 0:
            report.append("=" * 80)
            report.append("⚠️  CATEGORIES WITH ISSUES")
            report.append("=" * 80)
            report.append("")
            
            for result in results:
                if not result["issues"]:
                    continue
                
                report.append(f"📁 {result['full_path']}")
                report.append(f"   ID: {result['taxonomy_id']}")
                report.append(f"   Children: {result['children_count']}")
                report.append("")
                
                # List issues
                for issue in result["issues"]:
                    severity_icon = "🔴" if issue["severity"] == "high" else "🟡"
                    report.append(f"   {severity_icon} [{issue['type'].upper()}] {issue['message']}")
                    if "reason" in issue:
                        report.append(f"      Reason: {issue['reason']}")
                report.append("")
                
                # List suggestions
                if result["suggestions"]:
                    report.append("   💡 SUGGESTIONS:")
                    for suggestion in result["suggestions"]:
                        if suggestion["type"] == "move_category":
                            report.append(f"      • Move '{suggestion['category']}' from '{suggestion['current_parent']}' to '{suggestion['suggested_parent']}'")
                            report.append(f"        Reason: {suggestion['reason']}")
                        elif suggestion["type"] == "add_description":
                            report.append(f"      • Add description for '{suggestion['category']}'")
                        else:
                            report.append(f"      • {suggestion.get('suggestion', 'Unknown suggestion')}")
                    report.append("")
        
        report.append("=" * 80)
        report.append("END OF REPORT")
        report.append("=" * 80)
        
        return "\n".join(report)


def main():
    """
    Interactive taxonomy maintenance tool
    """
    print("=" * 80)
    print("🔧 TAXONOMY MAINTENANCE AGENT")
    print("=" * 80)
    print()
    print("This tool performs BFS traversal of your taxonomy tree and analyzes")
    print("each branch for issues and improvement opportunities.")
    print()
    
    session = get_session()
    agent = TaxonomyMaintenanceAgent(session)
    
    # Get root categories
    roots = agent.get_root_categories()
    
    print("📁 Available root categories:")
    for i, root in enumerate(roots, 1):
        child_count = len(agent.get_children(root.id))
        desc_status = "✅" if root.description else "❌"
        print(f"   {i}. {root.label} (ID: {root.id}, {child_count} children) {desc_status}")
    print()
    
    # Choose starting point
    print("Options:")
    print("   1. Analyze ALL categories (full BFS from all roots)")
    print("   2. Analyze a SPECIFIC category and its descendants")
    print()
    
    choice = input("Enter your choice (1 or 2): ").strip()
    
    start_id = None
    max_depth = None
    
    if choice == "2":
        # Choose specific category
        print()
        category_choice = input("Enter the category number from the list above: ").strip()
        try:
            idx = int(category_choice) - 1
            if 0 <= idx < len(roots):
                start_id = roots[idx].id
                print(f"✅ Starting from: {roots[idx].label}")
            else:
                print("❌ Invalid choice, analyzing all categories")
        except ValueError:
            print("❌ Invalid input, analyzing all categories")
    
    # Ask for max depth
    print()
    depth_choice = input("Maximum depth to analyze (press Enter for unlimited): ").strip()
    if depth_choice:
        try:
            max_depth = int(depth_choice)
            print(f"✅ Max depth: {max_depth}")
        except ValueError:
            print("❌ Invalid depth, using unlimited")
    
    print()
    print("=" * 80)
    print("🚀 Starting analysis...")
    print("=" * 80)
    
    # Perform analysis
    results = agent.bfs_traverse_and_analyze(
        start_taxonomy_id=start_id,
        max_depth=max_depth
    )
    
    # Generate report
    report = agent.generate_report(results)
    
    # Print report
    print()
    print(report)
    
    # Save report to file
    import datetime
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"taxonomy_maintenance_report_{timestamp}.txt"
    
    with open(filename, 'w', encoding='utf-8') as f:
        f.write(report)
    
    print()
    print(f"📄 Report saved to: {filename}")
    
    session.close()


if __name__ == "__main__":
    import app.assistant.tests.test_setup
    main()

