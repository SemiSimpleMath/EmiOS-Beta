"""
KG Edit Tool

Backs the kg_create_node and kg_update_node tool names. The destructive
ops (kg_delete_node, kg_delete_edge) and kg_create_edge moved to
KGMutatorTool 2026-06-10 — that path is audited (kg_revision_log,
required reason, dry_run, locked_by_user_at enforcement); this one
is not.
"""
from typing import Dict, Any
import uuid
import json

from app.assistant.lib.core_tools.base_tool.base_tool import BaseTool
from app.assistant.lib.core_tools.tool_error_protocol import make_tool_error
from app.assistant.utils.pydantic_classes import ToolResult, ToolMessage
from app.assistant.kg_core.kg_utils.knowledge_graph_utils import KnowledgeGraphUtils
from app.models.base import get_session
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)

class KGEdit(BaseTool):
    """
    Unified tool for knowledge graph editing operations.
    Handles create, update, and delete operations for nodes and edges.
    """

    def __init__(self):
        super().__init__('kg_edit')
        self.do_lazy_init = True
        self.session = None

    def lazy_init(self):
        self.session = get_session()
        self.kg_utils = KnowledgeGraphUtils(self.session)
        self.do_lazy_init = False

    def execute(self, tool_message: ToolMessage) -> ToolResult:
        logger.info(f"🚀 [KGEdit] EXECUTE ENTRY - tool_message: {type(tool_message)}")
        logger.info(f"🚀 [KGEdit] tool_message.tool_data: {json.dumps(tool_message.tool_data, ensure_ascii=False)}")
        
        if self.do_lazy_init:
            logger.info(f"🚀 [KGEdit] Lazy initializing...")
            self.lazy_init()

        try:
            logger.info("🚀 [KGEdit] Executing KGEdit")
            arguments = tool_message.tool_data.get('arguments', {})
            tool_name = tool_message.tool_name or (tool_message.tool_data or {}).get('tool_name') or 'unknown_tool'
            
            logger.info(f"🚀 [KGEdit] tool_name: {tool_name}")
            logger.info(f"🚀 [KGEdit] arguments: {json.dumps(arguments, ensure_ascii=False)}")

            if not tool_name:
                logger.error(f"❌ [KGEdit] Missing tool_name in tool_data!")
                raise ValueError("Missing tool_name in tool_data.")

            handler_method = getattr(self, f"handle_{tool_name}", None)
            logger.info(f"🚀 [KGEdit] handler_method: {handler_method}")
            
            if not handler_method:
                logger.error(f"❌ [KGEdit] Unsupported tool_name: {tool_name}")
                raise ValueError(f"Unsupported tool_name: {tool_name}")

            logger.info(f"🚀 [KGEdit] Calling handler_method with arguments: {json.dumps(arguments, ensure_ascii=False)}")
            result = handler_method(arguments, tool_message)
            logger.info(f"🚀 [KGEdit] handler_method returned: {result}")
            return result

        except Exception as e:
            logger.error("❌ [KGEdit] KGEdit execution failed")
            logger.debug("kGEdit] KGEdit execution failed exception details", exc_info=True)
            return self.publish_error(
                make_tool_error(
                    error_code="kg_edit_execute_failed",
                    message=str(e),
                    abort_policy="abort_tool",
                    retryable=False,
                )
            )

    def publish_result(self, result: ToolResult) -> ToolResult:
        return result

    def publish_error(self, error_result: ToolResult) -> ToolResult:
        return error_result

    # ---------------------- INDIVIDUAL TOOL HANDLERS ----------------------

    def handle_kg_update_node(self, arguments: Dict[str, Any], tool_message: ToolMessage) -> ToolResult:
        """Handle kg_update_node tool calls."""
        logger.info(f"🔍 [kg_update_node] ENTRY - Received tool call")
        logger.info(f"🔍 [kg_update_node] tool_message.tool_data: {json.dumps(tool_message.tool_data, ensure_ascii=False)}")
        logger.info(f"🔍 [kg_update_node] arguments: {json.dumps(arguments, ensure_ascii=False)}")
        logger.info(f"🔍 [kg_update_node] tool_message type: {type(tool_message)}")
        logger.info(f"🔍 [kg_update_node] arguments type: {type(arguments)}")
        
        # Check if we have the right arguments structure
        if not arguments:
            logger.warning(f"⚠️ [kg_update_node] No arguments provided, trying tool_message.tool_data")
            arguments = tool_message.tool_data or {}
            logger.info(f"🔍 [kg_update_node] Using tool_message.tool_data as arguments: {json.dumps(arguments, ensure_ascii=False)}")
        
        # Check for node_id
        node_id_str = arguments.get("node_id")
        logger.info(f"🔍 [kg_update_node] node_id_str: {node_id_str} (type: {type(node_id_str)})")
        
        if not node_id_str:
            logger.error(f"❌ [kg_update_node] No node_id found in arguments!")
            logger.error(f"❌ [kg_update_node] Available keys: {list(arguments.keys())}")
            return ToolResult(
                result_type="kg_update_node_error",
                content="No node_id provided",
                data={"success": False, "error": "No node_id provided"}
            )
        
        # Check for update fields
        update_fields = ["label", "description", "aliases", "attributes", "start_date", "end_date", "start_date_confidence", "end_date_confidence", "valid_during"]
        found_fields = {}
        for field in update_fields:
            value = arguments.get(field)
            if value is not None:
                found_fields[field] = value
                logger.info(f"🔍 [kg_update_node] Found field '{field}': {value} (type: {type(value)})")
        
        logger.info(f"🔍 [kg_update_node] Found {len(found_fields)} fields to update: {list(found_fields.keys())}")
        
        # Try to convert node_id to UUID
        try:
            node_id = uuid.UUID(node_id_str)
            logger.info(f"🔍 [kg_update_node] Successfully converted node_id to UUID: {node_id}")
        except ValueError as e:
            logger.error(f"❌ [kg_update_node] Failed to convert node_id to UUID: {e}")
            return ToolResult(
                result_type="kg_update_node_error",
                content=f"Invalid node_id format: {e}",
                data={"success": False, "error": f"Invalid node_id format: {e}"}
            )
        
        logger.info(f"🔍 [kg_update_node] Delegating to handle_kg_node_edit...")
        result = self.handle_kg_node_edit(arguments, tool_message)
        logger.info(f"🔍 [kg_update_node] handle_kg_node_edit returned: {result}")
        return result

    def handle_kg_create_node(self, arguments: Dict[str, Any], tool_message: ToolMessage) -> ToolResult:
        """Handle kg_create_node tool calls."""
        # For create operations, we need to implement this
        return self.handle_kg_node_create(arguments, tool_message)

    # ---------------------- NODE CREATE OPERATIONS ----------------------

    def handle_kg_node_create(self, arguments: Dict[str, Any], tool_message: ToolMessage) -> ToolResult:
        """Create a new node in the knowledge graph."""
        try:
            logger.debug(f"[kg_node_create] Received arguments: {json.dumps(arguments, ensure_ascii=False)}")
            
            # Extract required fields
            label = arguments.get("label")
            if not label:
                raise ValueError("Missing required field: label")

            # Extract optional fields
            node_type = arguments.get("node_type", "Entity")
            description = arguments.get("description")
            aliases = arguments.get("aliases", [])
            attributes = arguments.get("attributes", {})
            category = arguments.get("category")
            start_date = arguments.get("start_date")
            end_date = arguments.get("end_date")
            start_date_confidence = arguments.get("start_date_confidence")
            end_date_confidence = arguments.get("end_date_confidence")
            valid_during = arguments.get("valid_during")
            confidence = arguments.get("confidence")
            importance = arguments.get("importance")
            source = arguments.get("source")

            # Build node data
            node_data = {
                "node_type": node_type,
                "label": label,
                "aliases": aliases,
                "description": description,
                "attributes": attributes,
                "category": category,
                "valid_during": valid_during,
                "start_date": start_date,
                "end_date": end_date,
                "start_date_confidence": start_date_confidence,
                "end_date_confidence": end_date_confidence,
                "confidence": confidence,
                "importance": importance,
                "source": source,
            }

            # Remove None values
            node_data = {k: v for k, v in node_data.items() if v is not None}

            logger.debug(f"[kg_node_create] Creating node with data: {json.dumps(node_data, ensure_ascii=False)}")
            
            # Create the node
            new_node, status = self.kg_utils.create_node(**node_data)
            
            # Commit changes
            self.session.commit()
            logger.info(f"✅ Successfully created node '{new_node.label}' (ID: {new_node.id})")

            result_payload = {
                "success": True,
                "node_id": str(new_node.id),
                "label": new_node.label,
                "node_type": new_node.node_type,
                "message": f"Successfully created node '{new_node.label}'"
            }

            return self.publish_result(
                ToolResult(
                    result_type="kg_create_node",
                    content=f"Created node '{new_node.label}'",
                    data=result_payload
                )
            )

        except Exception as e:
            self.session.rollback()
            logger.error("❌ Failed to create node: %s", e)
            logger.debug("failed to create node exception details", exc_info=True)
            return self.publish_error(
                ToolResult(
                    result_type="kg_create_node_error",
                    content=f"Failed to create node: {e}",
                    data={"success": False, "error": str(e)}
                )
            )

    # ---------------------- NODE EDIT OPERATIONS ----------------------

    def handle_kg_node_edit(self, arguments: Dict[str, Any], tool_message: ToolMessage) -> ToolResult:
        """Edit/update an existing node in the knowledge graph."""
        try:
            logger.info(f"🔧 [kg_node_edit] ENTRY - Received arguments: {json.dumps(arguments, ensure_ascii=False)}")
            
            node_id_str = arguments.get("node_id")
            logger.info(f"🔧 [kg_node_edit] node_id_str: {node_id_str} (type: {type(node_id_str)})")
            
            if not node_id_str:
                logger.error(f"❌ [kg_node_edit] Missing required field: node_id")
                raise ValueError("Missing required field: node_id")

            # Convert to UUID
            try:
                node_id = uuid.UUID(node_id_str)
                logger.info(f"🔧 [kg_node_edit] Successfully converted to UUID: {node_id}")
            except ValueError as e:
                logger.error(f"❌ [kg_node_edit] Invalid node_id format: {e}")
                raise ValueError(f"Invalid node_id format: {node_id_str}")

            # Collect update fields (skip None and empty values)
            update_fields = [
                "label", "semantic_label", "description", "aliases", "category", "attributes",
                "start_date", "end_date", "start_date_confidence", "end_date_confidence",
                "valid_during"
            ]
            
            updates = {}
            for field in update_fields:
                value = arguments.get(field)
                logger.info(f"🔧 [kg_node_edit] Checking field '{field}': {value} (type: {type(value)})")
                if value is None:
                    logger.info(f"🔧 [kg_node_edit] Skipping '{field}' - None value")
                    continue
                if isinstance(value, str) and value.strip() == "":
                    logger.info(f"🔧 [kg_node_edit] Skipping '{field}' - empty string")
                    continue
                if isinstance(value, (list, dict)) and len(value) == 0:
                    logger.info(f"🔧 [kg_node_edit] Skipping '{field}' - empty list/dict")
                    continue
                updates[field] = value
                logger.info(f"🔧 [kg_node_edit] Added '{field}' to updates: {value}")
            

            logger.info(f"🔧 [kg_node_edit] Final updates: {json.dumps(updates, ensure_ascii=False)}")
            
            if not updates:
                logger.error(f"❌ [kg_node_edit] No fields to update!")
                raise ValueError("No fields to update")

            # Get the node first
            logger.info(f"🔧 [kg_node_edit] Looking up node with ID: {node_id}")
            node = self.kg_utils.get_node_by_id(node_id)
            if not node:
                logger.error(f"❌ [kg_node_edit] Node not found: {node_id}")
                raise ValueError(f"Node not found: {node_id}")
            
            logger.info(f"🔧 [kg_node_edit] Found node: {node.label} (type: {node.node_type})")

            old_label = node.label
            logger.info(f"🔧 [kg_node_edit] Updating node '{old_label}' (ID: {node_id}) with {len(updates)} fields")

            # Update the node
            logger.info(f"🔧 [kg_node_edit] Calling kg_utils.update_node with node_id: {node_id} (type: {type(node_id)})")
            logger.info(f"🔧 [kg_node_edit] Calling kg_utils.update_node with updates: {json.dumps(updates, ensure_ascii=False)}")
            
            try:
                updated_node = self.kg_utils.update_node(node_id, updates)
                logger.info(f"🔧 [kg_node_edit] kg_utils.update_node returned: {updated_node}")
                logger.info(f"🔧 [kg_node_edit] updated_node type: {type(updated_node)}")
                if updated_node:
                    logger.info(f"🔧 [kg_node_edit] updated_node.label: {updated_node.label}")
                    logger.info(f"🔧 [kg_node_edit] updated_node.id: {updated_node.id}")
            except Exception as e:
                logger.error("❌ [kg_node_edit] Exception in kg_utils.update_node: %s", e)
                logger.debug("kg_node_edit] Exception in kg_utils.update_node exception details", exc_info=True)
                raise
            
            if not updated_node:
                logger.error(f"❌ [kg_node_edit] Update failed for node: {node_id}")
                raise ValueError(f"Update failed for node: {node_id}")
            
            logger.info(f"🔧 [kg_node_edit] Update successful, updated_node: {updated_node.label}")

            # Commit changes
            logger.info(f"🔧 [kg_node_edit] Committing transaction...")
            self.session.commit()
            logger.info(f"🔧 [kg_node_edit] Transaction committed successfully")
            logger.info(f"✅ Successfully updated node '{updated_node.label}'")

            # Build result message showing requested values
            requested_label = updates.get("label")
            if requested_label is not None:
                human_message = f"Successfully updated label to '{requested_label}'"
            else:
                # Show specific changes made
                change_details = []
                for field, value in updates.items():
                    if field == "label":
                        change_details.append(f"label to '{value}'")
                    elif field == "description":
                        change_details.append(f"description to '{value[:50]}{'...' if len(str(value)) > 50 else ''}'")
                    elif field == "aliases":
                        change_details.append(f"aliases to {value}")
                    elif field == "start_date":
                        change_details.append(f"start_date to '{value}'")
                    elif field == "end_date":
                        change_details.append(f"end_date to '{value}'")
                    elif field == "valid_during":
                        change_details.append(f"valid_during to '{value}'")
                    else:
                        change_details.append(f"{field} to '{value}'")
                
                human_message = f"Successfully updated: {', '.join(change_details)}"

            # Show requested values prominently for the calling agent
            result_payload = {
                "success": True,
                "node_id": str(updated_node.id),
                "label": requested_label if requested_label is not None else updated_node.label,
                "node_type": updated_node.node_type,
                "updated_fields": list(updates.keys()),
                "requested": {k: v for k, v in updates.items()},
                "applied_changes": {k: v for k, v in updates.items()},  # Explicitly show what was applied
                "message": human_message,
            }

            logger.info(f"🔧 [kg_node_edit] Returning result: {json.dumps(result_payload, ensure_ascii=False)}")
            return self.publish_result(
                ToolResult(
                    result_type="kg_node_edit",
                    content=human_message,
                    data=result_payload
                )
            )

        except Exception as e:
            logger.error("❌ [kg_node_edit] Failed to edit node: %s", e)
            logger.debug("kg_node_edit] Failed to edit node exception details", exc_info=True)
            self.session.rollback()
            return self.publish_error(
                ToolResult(
                    result_type="kg_node_edit_error",
                    content=f"Failed to edit node: {e}",
                    data={"success": False, "error": str(e)}
                )
            )

    def handle_kg_edge_edit(self, arguments: Dict[str, Any], tool_message: ToolMessage) -> ToolResult:
        """Edit/update an existing edge in the knowledge graph."""
        try:
            logger.debug(f"[kg_edge_edit] Received arguments: {json.dumps(arguments, ensure_ascii=False)}")
            
            edge_id_str = arguments.get("edge_id")
            if not edge_id_str:
                raise ValueError("Missing required field: edge_id")

            # Convert to UUID
            try:
                edge_id = uuid.UUID(edge_id_str)
            except ValueError:
                raise ValueError(f"Invalid edge_id format: {edge_id_str}")

            # Collect update fields
            update_fields = [
                "relationship_type", "attributes", "start_date", "end_date",
                "start_date_confidence", "end_date_confidence", "valid_during"
            ]
            
            updates = {}
            for field in update_fields:
                value = arguments.get(field)
                if value is None:
                    continue
                if isinstance(value, str) and value.strip() == "":
                    continue
                if isinstance(value, (list, dict)) and len(value) == 0:
                    continue
                updates[field] = value

            if not updates:
                raise ValueError("No fields to update")

            logger.debug(f"[kg_edge_edit] Computed updates: {json.dumps(updates, ensure_ascii=False)}")

            # Get the edge first using direct SQLAlchemy query
            from app.assistant.kg.db.knowledge_graph_db_sqlite import Edge
            edge = self.session.query(Edge).filter(Edge.id == edge_id).first()
            if not edge:
                raise ValueError(f"Edge not found: {edge_id}")

            old_type = edge.relationship_type
            logger.info(f"🔄 Updating edge '{old_type}' (ID: {edge_id}) with {len(updates)} fields")

            # Update the edge fields directly
            for field, value in updates.items():
                if hasattr(edge, field):
                    setattr(edge, field, value)
                else:
                    logger.warning(f"Edge does not have field '{field}', skipping")

            # Commit changes
            self.session.commit()
            logger.info(f"✅ Successfully updated edge '{edge.relationship_type}'")

            result_payload = {
                "success": True,
                "edge_id": str(edge.id),
                "relationship_type": edge.relationship_type,
                "updated_fields": list(updates.keys()),
                "message": f"Successfully updated edge '{edge.relationship_type}'"
            }

            return self.publish_result(
                ToolResult(
                    result_type="kg_edge_edit",
                    content=f"Updated edge '{edge.relationship_type}'",
                    data=result_payload
                )
            )

        except Exception as e:
            self.session.rollback()
            logger.error("❌ Failed to edit edge: %s", e)
            logger.debug("failed to edit edge exception details", exc_info=True)
            return self.publish_error(
                ToolResult(
                    result_type="kg_edge_edit_error",
                    content=f"Failed to edit edge: {e}",
                    data={"success": False, "error": str(e)}
                )
            )


if __name__ == "__main__":
    # Test the tool
    tool = KGEdit()
    
    # Test edit node
    edit_msg = ToolMessage(
        tool_name="kg_node_edit",
        tool_data={
            "tool_name": "kg_node_edit",
            "arguments": {
                "node_id": "test-uuid",
                "label": "Updated Label"
            }
        }
    )
    result = tool.execute(edit_msg)
    print("Edit node result:", result)
