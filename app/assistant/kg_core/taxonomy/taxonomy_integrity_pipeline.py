"""
Taxonomy Integrity Pipeline

Validates and maintains the integrity of the taxonomy structure by:
1. Detecting duplicate or near-duplicate taxonomy types
2. Identifying misplaced categories
3. Suggesting merges for redundant types
4. Validating parent-child relationships
5. Checking for orphaned branches

This pipeline can process the entire taxonomy or specific branches (e.g., Entity, Event, State).
"""

import sys
import json
from typing import Dict, List, Any, Optional
from sqlalchemy import text
from app.assistant.tests.test_setup import initialize_services


class TaxonomyIntegrityAgent:
    """
    Agent that analyzes taxonomy structure for integrity issues.
    
    Uses LLM to:
    - Detect semantic duplicates (e.g., "email address" vs "email_address" vs "email")
    - Suggest merges with reasoning
    - Identify misplaced children (e.g., "dog" under "vehicle")
    - Validate descriptions and relationships
    """
    
    def __init__(self):
        from app.assistant.ServiceLocator.service_locator import DI
        
        # Use the dedicated taxonomy integrity validator agent
        self.agent = DI.agent_factory.create_agent("knowledge_graph_add::taxonomy_integrity_validator")
        
    def analyze_branch(self, branch_data: Dict[str, Any], branch_name: str) -> Dict[str, Any]:
        """
        Analyze a taxonomy branch for integrity issues.
        
        Args:
            branch_data: The taxonomy branch as a nested dict
            branch_name: Name of the root category (e.g., "Entity", "Event")
            
        Returns:
            Analysis results with duplicates, misplacements, and merge suggestions
        """
        
        # Flatten the branch to get all categories with their paths
        categories = self._flatten_branch(branch_data, parent_path="")
        
        print(f"\n🔍 Analyzing {branch_name} branch...")
        print(f"   Total categories: {len(categories)}")
        
        # Prepare the input context for the agent
        agent_input = self._build_integrity_prompt(categories, branch_name)
        
        # Call the agent using action_handler (like in kg_pipeline)
        print(f"   Calling LLM for analysis...")
        from app.assistant.utils.pydantic_classes import Message
        result = self.agent.action_handler(Message(agent_input=agent_input))
        
        # Parse agent response
        if result and hasattr(result, 'data') and result.data:
            response = result.data
        else:
            response = str(result)
        
        return {
            "branch_name": branch_name,
            "total_categories": len(categories),
            "analysis": response,
            "categories": categories
        }
    
    def _flatten_branch(self, node: Dict[str, Any], parent_path: str) -> List[Dict[str, Any]]:
        """
        Recursively flatten a taxonomy branch into a list of categories with paths.
        
        Args:
            node: Current taxonomy node
            parent_path: Path to this node from root
            
        Returns:
            List of category dicts with label, path, description, children
        """
        categories = []
        
        label = node.get("label", "")
        description = node.get("description", "")
        taxonomy_id = node.get("id", "")
        
        # Build current path
        current_path = f"{parent_path} > {label}" if parent_path else label
        
        # Add this category
        category_info = {
            "id": taxonomy_id,
            "label": label,
            "path": current_path,
            "description": description,
            "children_labels": [child.get("label", "") for child in node.get("children", [])]
        }
        categories.append(category_info)
        
        # Recurse into children
        for child in node.get("children", []):
            categories.extend(self._flatten_branch(child, current_path))
        
        return categories
    
    def _build_integrity_prompt(self, categories: List[Dict[str, Any]], branch_name: str) -> Dict[str, Any]:
        """
        Build context for the LLM to analyze taxonomy integrity.
        
        Args:
            categories: List of category dicts
            branch_name: Name of the branch being analyzed
            
        Returns:
            Context dict for the agent
        """
        
        # Format categories for the prompt
        categories_text = "\n\n".join([
            f"ID: {cat['id']}\n"
            f"Label: {cat['label']}\n"
            f"Path: {cat['path']}\n"
            f"Description: {cat['description'] or '(no description)'}\n"
            f"Children: {', '.join(cat['children_labels']) if cat['children_labels'] else '(none)'}"
            for cat in categories
        ])
        
        return {
            "branch_name": branch_name,
            "categories_text": categories_text,
            "total_categories": len(categories)
        }


class TaxonomyIntegrityPipeline:
    """
    Pipeline to validate and maintain taxonomy integrity.
    """
    
    def __init__(self):
        self.agent = TaxonomyIntegrityAgent()
        self.session = None
        self.category_map = {}  # Cache for quick category lookups
        
    def load_taxonomy_json(self, branch_name: Optional[str] = None) -> Dict[str, Any]:
        """
        Load taxonomy from database and convert to JSON structure.
        
        Args:
            branch_name: If provided, only load this branch (e.g., "Entity", "Event")
            
        Returns:
            Taxonomy as nested dict
        """
        from app.models.base import get_session
        
        self.session = get_session()
        
        print(f"📥 Loading taxonomy from database...")
        
        # Get the root node(s)
        if branch_name:
            print(f"   Filtering for branch: {branch_name}")
            
            # Get all root branches first to check available options
            all_roots = self.session.execute(
                text("SELECT id, label, description FROM taxonomy WHERE parent_id IS NULL ORDER BY label")
            ).fetchall()
            
            # Try case-insensitive match
            root = None
            for r in all_roots:
                if r[1].lower() == branch_name.lower():
                    root = r
                    break
            
            if not root:
                print(f"❌ Branch '{branch_name}' not found")
                print(f"\n📋 Available branches:")
                for r in all_roots:
                    print(f"   - {r[1]}")
                return None
            
            print(f"   Found branch: {root[1]} (ID: {root[0]})")
            return self._build_tree(root[0])
        else:
            # Get all root nodes
            roots = self.session.execute(
                text("SELECT id, label, description FROM taxonomy WHERE parent_id IS NULL ORDER BY label")
            ).fetchall()
            
            print(f"   Loading {len(roots)} root branches...")
            for root in roots:
                print(f"      - {root[1]}")
            
            return {
                "roots": [self._build_tree(root[0]) for root in roots]
            }
    
    def _build_category_map(self, node: Dict[str, Any], parent_path: str = ""):
        """
        Recursively build a map of category_id -> full info (label, path, description, parent).
        
        Args:
            node: Current taxonomy node
            parent_path: Path to this node from root
        """
        label = node.get("label", "")
        taxonomy_id = node.get("id", "")
        description = node.get("description", "")
        
        # Build current path
        current_path = f"{parent_path} > {label}" if parent_path else label
        
        # Store in map
        self.category_map[taxonomy_id] = {
            "id": taxonomy_id,
            "label": label,
            "path": current_path,
            "description": description
        }
        
        # Recurse into children
        for child in node.get("children", []):
            self._build_category_map(child, current_path)
    
    def format_issue_for_team_manager(self, issue: Dict[str, Any]) -> Dict[str, str]:
        """
        Convert a taxonomy_integrity_validator issue into a format ready for taxonomy_team_manager.
        
        Enriches the issue with full context for all referenced categories.
        
        Args:
            issue: Issue dict from taxonomy_integrity_validator
            
        Returns:
            Dict with 'task' and 'info' fields for taxonomy_team_manager
        """
        # Extract basic info
        problem = issue.get("problem", "Unknown problem")
        actions = issue.get("actions", [])
        confidence = issue.get("confidence", 0.0)
        category_ids = issue.get("category_ids", [])
        labels = issue.get("labels", [])
        paths = issue.get("paths", [])
        
        # Group categories by label to show duplicates clearly
        label_groups = {}
        for i, cat_id in enumerate(category_ids):
            label = labels[i] if i < len(labels) else "unknown"
            if label not in label_groups:
                label_groups[label] = []
            
            if cat_id in self.category_map:
                cat_info = self.category_map[cat_id]
                label_groups[label].append({
                    "id": cat_id,
                    "path": cat_info['path'],
                    "description": cat_info['description'],
                    "parent": self._get_parent_label(cat_info['path'])
                })
            else:
                # Fallback - use path from issue if available
                path = paths[i] if i < len(paths) else "unknown"
                label_groups[label].append({
                    "id": cat_id,
                    "path": path,
                    "description": "(no description)",
                    "parent": self._get_parent_label(path)
                })
        
        # Build category details grouped by label
        category_details = []
        for label, instances in label_groups.items():
            if len(instances) > 1:
                # Multiple instances = duplicates
                category_details.append(f"\n  [CATEGORY] '{label}' (DUPLICATE - {len(instances)} instances):")
                for inst in instances:
                    category_details.append(
                        f"    - ID: {inst['id']}\n"
                        f"      Path: {inst['path']}\n"
                        f"      Parent: {inst['parent']}\n"
                        f"      Description: {inst['description'] or '(no description)'}"
                    )
            else:
                # Single instance
                inst = instances[0]
                category_details.append(
                    f"\n  [CATEGORY] '{label}' (ID: {inst['id']})\n"
                    f"    Path: {inst['path']}\n"
                    f"    Parent: {inst['parent']}\n"
                    f"    Description: {inst['description'] or '(no description)'}"
                )
        
        # Parse and explain actions
        action_details = []
        for i, action in enumerate(actions, 1):
            explanation = self._explain_action(action, label_groups)
            action_details.append(f"  {i}. {action}\n     => {explanation}")
        
        # Create task (short summary)
        task = problem[:200] + "..." if len(problem) > 200 else problem
        
        # Create info (full context)
        info = f"""
PROBLEM:
{problem}

AFFECTED CATEGORIES:
{chr(10).join(category_details)}

ACTIONS TO TAKE (IN ORDER):
{chr(10).join(action_details)}

NOTE: All category IDs, paths, and parent relationships are provided above.
The team manager should verify each action before execution.
"""
        
        return {
            "task": task,
            "info": info.strip()
        }
    
    def _get_parent_label(self, path: str) -> str:
        """Extract the parent label from a path string."""
        if not path or ' > ' not in path:
            return "(root)"
        parts = path.split(' > ')
        return parts[-2] if len(parts) > 1 else "(root)"
    
    def _explain_action(self, action: str, label_groups: Dict[str, List[Dict]]) -> str:
        """
        Parse an action string and provide a human-readable explanation.
        
        Args:
            action: Action string like "merge_categories(746, 492)"
            label_groups: Grouped category info for context
            
        Returns:
            Human-readable explanation
        """
        import re
        
        # Parse merge_categories
        merge_match = re.match(r'merge_categories\((\d+),\s*(\d+)\)', action)
        if merge_match:
            source_id = int(merge_match.group(1))
            dest_id = int(merge_match.group(2))
            
            # Find labels
            source_label = self._find_label_for_id(source_id, label_groups)
            dest_label = self._find_label_for_id(dest_id, label_groups)
            
            # Get paths
            source_path = self.category_map.get(source_id, {}).get('path', 'unknown')
            dest_path = self.category_map.get(dest_id, {}).get('path', 'unknown')
            
            return f"Merge '{source_label}' (ID {source_id}) at '{source_path}' INTO '{dest_label}' (ID {dest_id}) at '{dest_path}'"
        
        # Parse move_category
        move_match = re.match(r'move_category\((\d+),\s*(\d+)\)', action)
        if move_match:
            cat_id = int(move_match.group(1))
            new_parent_id = int(move_match.group(2))
            
            cat_label = self._find_label_for_id(cat_id, label_groups)
            parent_label = self.category_map.get(new_parent_id, {}).get('label', f'ID {new_parent_id}')
            
            return f"Move '{cat_label}' (ID {cat_id}) under '{parent_label}' (ID {new_parent_id})"
        
        # Parse rename_category
        rename_match = re.match(r'rename_category\((\d+),\s*["\'](.+?)["\']\)', action)
        if rename_match:
            cat_id = int(rename_match.group(1))
            new_name = rename_match.group(2)
            
            old_label = self._find_label_for_id(cat_id, label_groups)
            
            return f"Rename '{old_label}' (ID {cat_id}) to '{new_name}'"
        
        # Parse update_description
        desc_match = re.match(r'update_description\((\d+),\s*["\'](.+?)["\']\)', action)
        if desc_match:
            cat_id = int(desc_match.group(1))
            new_desc = desc_match.group(2)
            
            cat_label = self._find_label_for_id(cat_id, label_groups)
            
            return f"Update description for '{cat_label}' (ID {cat_id}) to: '{new_desc[:5000]}...'"
        
        # Parse create_category
        create_match = re.match(r'create_category\((\d+),\s*["\'](.+?)["\']\s*,\s*["\'](.+?)["\']\)', action)
        if create_match:
            parent_id = int(create_match.group(1))
            new_label = create_match.group(2)
            description = create_match.group(3)
            
            parent_label = self.category_map.get(parent_id, {}).get('label', f'ID {parent_id}')
            parent_path = self.category_map.get(parent_id, {}).get('path', 'unknown')
            
            return f"Create new category '{new_label}' under '{parent_label}' (ID {parent_id}) at path '{parent_path} > {new_label}'"
        
        # Fallback
        return "Execute this action"
    
    def _find_label_for_id(self, cat_id: int, label_groups: Dict[str, List[Dict]]) -> str:
        """Find the label for a given category ID."""
        for label, instances in label_groups.items():
            for inst in instances:
                if inst['id'] == cat_id:
                    return label
        
        # Fallback to cache
        return self.category_map.get(cat_id, {}).get('label', f'ID {cat_id}')
    
    def _build_tree(self, taxonomy_id: int) -> Dict[str, Any]:
        """
        Recursively build taxonomy tree from a given node.
        
        Args:
            taxonomy_id: ID of the taxonomy node to start from
            
        Returns:
            Nested dict representing the tree
        """
        # Get this node
        node = self.session.execute(
            text("SELECT id, label, description FROM taxonomy WHERE id = :id"),
            {"id": taxonomy_id}
        ).first()
        
        if not node:
            return {}
        
        # Get children
        children = self.session.execute(
            text("SELECT id FROM taxonomy WHERE parent_id = :parent_id ORDER BY label"),
            {"parent_id": taxonomy_id}
        ).fetchall()
        
        return {
            "id": node[0],
            "label": node[1],
            "description": node[2],
            "children": [self._build_tree(child[0]) for child in children]
        }
    
    def run(self, branch_name: Optional[str] = None, output_file: Optional[str] = None):
        """
        Run the integrity pipeline.
        
        Args:
            branch_name: Optional branch to analyze (e.g., "Entity")
            output_file: Optional file to save results to
        """
        print("🚀 Starting Taxonomy Integrity Pipeline")
        print("="*80)
        
        # Load taxonomy
        taxonomy_data = self.load_taxonomy_json(branch_name)
        
        if not taxonomy_data:
            print("❌ Failed to load taxonomy")
            return
        
        # Analyze the branch(es)
        if branch_name:
            # Single branch analysis
            results = self.agent.analyze_branch(taxonomy_data, branch_name)
            all_results = [results]
            # Build category map for this branch
            self._build_category_map(taxonomy_data)
        else:
            # Analyze all root branches
            all_results = []
            for root in taxonomy_data["roots"]:
                branch_name = root["label"]
                results = self.agent.analyze_branch(root, branch_name)
                all_results.append(results)
                # Build category map for all branches
                self._build_category_map(root)
        
        # Print summary and format for team manager
        print("\n" + "="*80)
        print("📊 INTEGRITY ANALYSIS SUMMARY")
        print("="*80)
        
        formatted_issues = []  # Collect formatted issues for team manager
        
        for result in all_results:
            print(f"\n🌳 Branch: {result['branch_name']}")
            print(f"   Categories: {result['total_categories']}")
            
            analysis = result['analysis']
            if isinstance(analysis, dict) and 'issues' in analysis:
                issues = analysis['issues']
                print(f"   Issues found: {len(issues)}")
                
                # Format each issue for team manager
                for i, issue in enumerate(issues, 1):
                    print(f"\n   📋 Issue #{i}:")
                    print(f"      Problem: {issue.get('problem', 'N/A')[:100]}...")
                    print(f"      Actions: {len(issue.get('actions', []))} step(s)")
                    print(f"      Confidence: {issue.get('confidence', 0):.1%}")
                    
                    # Format for team manager
                    formatted = self.format_issue_for_team_manager(issue)
                    formatted_issues.append(formatted)
            else:
                print(f"   Analysis: {analysis}")
        
        # Save results if requested (before cleanup)
        if output_file:
            print(f"\n💾 Saving results to {output_file}...")
            
            # Add formatted issues to results
            output_data = {
                "analysis_results": all_results,
                "formatted_for_team_manager": formatted_issues
            }
            
            with open(output_file, 'w', encoding='utf-8') as f:
                json.dump(output_data, f, indent=2, default=str)
            print(f"✅ Results saved")
        
        # Cleanup
        if self.session:
            self.session.close()
        
        # Return formatted issues for processing
        return formatted_issues


def process_issues_with_team_manager(formatted_issues: List[Dict[str, str]]):
    """
    Process each issue one at a time with the taxonomy_team_manager.
    
    Args:
        formatted_issues: List of issues formatted for team manager (task + info)
    """
    from app.assistant.ServiceLocator.service_locator import DI
    from app.assistant.manager_runtime.services.scope_adapter import build_system_scope_context
    from app.assistant.utils.pydantic_classes import Message
    
    print("\n" + "="*80)
    print("🤖 PROCESSING ISSUES WITH TAXONOMY TEAM MANAGER")
    print("="*80)
    print(f"\nTotal issues to process: {len(formatted_issues)}")
    
    # Preload managers (only once)
    print("\n🔄 Preloading managers...")
    manager_registry = DI.manager_registry
    manager_registry.preload_all()
    print("✅ Preloading complete")
    
    # Get factory
    factory = DI.multi_agent_manager_factory
    
    # Ask once if user wants to approve all
    print("\nProcessing mode:")
    print("  1. Approve all issues automatically")
    print("  2. Review each issue individually")
    
    mode_choice = input("\nSelect mode (1-2): ").strip()
    auto_approve = (mode_choice == "1")
    
    if auto_approve:
        print("\n✅ Auto-approve mode enabled. Processing all issues...")
    
    for i, issue in enumerate(formatted_issues, 1):
        print(f"\n{'='*80}")
        print(f"📋 ISSUE {i}/{len(formatted_issues)}")
        print(f"{'='*80}")
        print(f"\n🎯 Task: {issue['task'][:100]}...")
        
        # Auto-approve or ask user
        if auto_approve:
            choice = "1"
        else:
            # Ask user if they want to process this issue
            print("\nOptions:")
            print("  1. Process this issue with team manager")
            print("  2. Skip this issue")
            print("  3. Exit (stop processing)")
            
            choice = input("\nSelect option (1-3): ").strip()
        
        if choice == "1":
            print(f"\n🚀 Calling taxonomy_team_manager for issue {i}...")
            print("-"*80)
            
            try:
                # Create the manager
                manager = factory.create_manager("taxonomy_team_manager")
                
                # Create request message
                request_message = Message(
                    data_type="agent_activation",
                    sender="User",
                    receiver="Delegator",
                    content="",
                    task=issue['task'],
                    information=issue['info'],
                    scope_context=build_system_scope_context(
                        owner_id="taxonomy_integrity_pipeline",
                        actor_id="taxonomy_integrity_pipeline",
                        surface="pipeline",
                        scope_id=f"scope::taxonomy_integrity::issue::{i}",
                    ),
                )
                
                # Call the manager
                result = DI.manager_invoker.invoke(manager, request_message)
                
                print("\n" + "-"*80)
                print("📊 TEAM MANAGER RESULT:")
                print("-"*80)
                print(result)
                
            except Exception as e:
                print(f"\n❌ Error processing issue {i}: {e}")
                import traceback
                traceback.print_exc()
                
                # Ask if user wants to continue
                cont = input("\nContinue to next issue? (y/n): ").strip().lower()
                if cont != 'y':
                    print("🛑 Stopping processing")
                    break
        
        elif choice == "2":
            print(f"⏭️  Skipping issue {i}")
            continue
        
        elif choice == "3":
            print("🛑 Stopping processing")
            break
        
        else:
            print("❌ Invalid choice, skipping issue")
            continue
    
    print("\n" + "="*80)
    print("✅ Finished processing issues")
    print("="*80)


def main():
    """
    Main entry point for the taxonomy integrity pipeline.
    """
    import argparse
    
    parser = argparse.ArgumentParser(description="Validate taxonomy integrity")
    parser.add_argument(
        "--branch",
        type=str,
        help="Specific branch to analyze (e.g., 'Entity', 'Event'). If not provided, analyzes all branches."
    )
    parser.add_argument(
        "--output",
        type=str,
        help="Output file for results (JSON format)"
    )
    
    args = parser.parse_args()
    
    # Initialize services
    print("🔧 Initializing services...")
    initialize_services()
    
    # Run pipeline
    pipeline = TaxonomyIntegrityPipeline()
    pipeline.run(branch_name=args.branch, output_file=args.output)


if __name__ == "__main__":
    # Allow running from IDE without CLI args
    if len(sys.argv) == 1:
        print("🔧 Initializing services...")
        initialize_services()
        
        print("\n" + "="*80)
        print("🏷️  TAXONOMY INTEGRITY PIPELINE")
        print("="*80)
        print("\nOptions:")
        print("1. Analyze specific branch (with team manager processing)")
        print("2. Analyze all branches (with team manager processing)")
        print("3. Analyze only (no team manager)")
        print("4. Exit")
        
        choice = input("\nSelect option (1-4): ").strip()
        
        if choice == "1":
            branch = input("Enter branch name (e.g., Entity, Event, State): ").strip()
            output = input("Save results to file? (leave empty to skip): ").strip() or None
            
            pipeline = TaxonomyIntegrityPipeline()
            issues = pipeline.run(branch_name=branch, output_file=output)
            
            # Process with team manager
            if issues:
                print(f"\n✅ Analysis complete. Found {len(issues)} issue(s).")
                process_choice = input("\nProcess issues with team manager? (y/n): ").strip().lower()
                if process_choice == 'y':
                    process_issues_with_team_manager(issues)
                else:
                    print("⏭️  Skipping team manager processing")
            else:
                print("\n✅ No issues found!")
            
        elif choice == "2":
            output = input("Save results to file? (leave empty to skip): ").strip() or None
            
            pipeline = TaxonomyIntegrityPipeline()
            issues = pipeline.run(branch_name=None, output_file=output)
            
            # Process with team manager
            if issues:
                print(f"\n✅ Analysis complete. Found {len(issues)} issue(s).")
                process_choice = input("\nProcess issues with team manager? (y/n): ").strip().lower()
                if process_choice == 'y':
                    process_issues_with_team_manager(issues)
                else:
                    print("⏭️  Skipping team manager processing")
            else:
                print("\n✅ No issues found!")
            
        elif choice == "3":
            # Analysis only, no team manager
            analyze_choice = input("\nAnalyze (1) specific branch or (2) all branches? ").strip()
            output = input("Save results to file? (leave empty to skip): ").strip() or None
            
            pipeline = TaxonomyIntegrityPipeline()
            if analyze_choice == "1":
                branch = input("Enter branch name: ").strip()
                issues = pipeline.run(branch_name=branch, output_file=output)
            else:
                issues = pipeline.run(branch_name=None, output_file=output)
            
            print(f"\n✅ Analysis complete. Found {len(issues) if issues else 0} issue(s).")
            
        elif choice == "4":
            print("👋 Goodbye!")
            sys.exit(0)
        else:
            print("❌ Invalid choice")
            sys.exit(1)
    else:
        # Run with CLI args
        main()

