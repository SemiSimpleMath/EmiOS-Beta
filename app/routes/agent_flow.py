from flask import Blueprint, request, jsonify, current_app

from app.assistant.agent_flow_manager.agent_flow_manager import AgentFlowManager
from app.assistant.utils.logging_config import get_logger

agent_flow_route_bp = Blueprint('agent_flow', __name__)
logger = get_logger(__name__)

# Agent-flow editor reads/writes agent configs — local-developer-only.
from app.routes._security import reject_if_not_local
agent_flow_route_bp.before_request(reject_if_not_local)


@agent_flow_route_bp.route('/agent_flow', methods=['POST'])
def agent_flow_route():
    try:
        data = request.get_json()
        manager = data.get('manager')

        agent_flow_manager = AgentFlowManager()
        config = agent_flow_manager.get_manager_config(manager)

        return jsonify(config), 200
    except Exception as e:
        logger.error("Error handling agent flow POST: %s", e)
        logger.debug("agent flow POST exception details", exc_info=True)
        return jsonify({'success': False, 'message': 'Error processing agent flow POST'}), 500

@agent_flow_route_bp.route('/agent_flow/load', methods=['GET'])
def load_agent_flow():
    try:
        manager = request.args.get('manager')
        logger.debug("Loading agent flow for manager: %s", manager)

        agent_flow_manager = AgentFlowManager()
        config = agent_flow_manager.get_manager_config(manager)

        return jsonify(config), 200

    except Exception as e:
        logger.error("Error loading agent flow: %s", e)
        logger.debug("agent flow load exception details", exc_info=True)
        return jsonify({'success': False, 'message': 'Error loading agent flow'}), 500

@agent_flow_route_bp.route('/agent_flow/agent_config', methods=['GET'])
def get_agent_config():
    try:
        manager = request.args.get('manager')
        agent_name = request.args.get('agent')

        agent_flow_manager = AgentFlowManager()
        agent_config = agent_flow_manager.get_agent_config(agent_name)

        return jsonify(agent_config), 200
    except Exception as e:
        logger.error("Failed to load agent config: %s", e)
        logger.debug("agent config load exception details", exc_info=True)
        return jsonify({'error': 'Could not load agent config'}), 500

@agent_flow_route_bp.route('/agent_flow/save_agent_config', methods=['POST'])
def save_agent_config():
    try:
        data = request.get_json()
        config = data['config']
        agent_name = config['name']
        logger.debug("Saving agent config for: %s", agent_name)
        agent_flow_manager = AgentFlowManager()
        agent_flow_manager.save_agent_config(config)
        return jsonify({'status': 'success'}), 200

    except Exception as e:
        logger.error("Failed to save agent config: %s", e)
        logger.debug("agent config save exception details", exc_info=True)
        return jsonify({'error': 'Could not save agent config'}), 500


@agent_flow_route_bp.route('/agent_flow/all_agent_configs', methods=['GET'])
def get_all_agent_configs():
    try:
        from app.assistant.ServiceLocator.service_locator import DI
        agent_registry = DI.agent_registry
        if not agent_registry.registry_loaded:
            return jsonify({'error': 'Agent registry not ready'}), 503

        agent_flow_manager = AgentFlowManager()
        all_configs = agent_flow_manager.get_all_agent_configs()

        return jsonify(all_configs), 200
    except Exception as e:
        logger.error("Failed to load all agent configs: %s", e)
        logger.debug("all agent configs exception details", exc_info=True)
        return jsonify({'error': 'Could not load agent configs'}), 500


@agent_flow_route_bp.route('/agent_flow/save_manager', methods=['POST'])
def save_manager():
    try:
        data = request.get_json()
        agent_flow_manager = AgentFlowManager()
        agent_flow_manager.save_manager(data)
        return jsonify({'status': 'success'}), 200

    except Exception as e:
        logger.error("Failed to save manager config: %s", e)
        logger.debug("save manager exception details", exc_info=True)
        return jsonify({'error': 'Could not save agent config'}), 500

@agent_flow_route_bp.route('/agent_flow/get_all_manager_configs', methods=['GET'])
def get_all_manager_configs():
    try:
        from app.assistant.ServiceLocator.service_locator import DI
        agent_registry = DI.agent_registry
        if not agent_registry.registry_loaded:
            return jsonify({'error': 'Agent registry not ready'}), 503

        agent_flow_manager = AgentFlowManager()
        all_manager_configs = agent_flow_manager.get_all_manager_configs()

        return jsonify(all_manager_configs), 200
    except Exception as e:
        logger.error("Failed to load all manager configs: %s", e)
        logger.debug("all manager configs exception details", exc_info=True)
        return jsonify({'error': 'Could not load manager configs'}), 500
