# KG Repair Pipeline

A dedicated pipeline for identifying and fixing problematic nodes in the knowledge graph through interactive user input.

## 🎯 Overview

The KG Repair Pipeline is a sequential data processing pipeline that:

1. **Analyzes** the knowledge graph to identify problematic nodes
2. **Critiques** findings to filter out false positives  
3. **Questions** users about missing data using the `ask_user` tool
4. **Implements** fixes based on user responses

## 🏗️ Architecture

```
Input: Knowledge Graph
    ↓
[Analyzer] → List of ProblematicNodes
    ↓
[Critic] → Validated/Filtered ProblematicNodes  
    ↓
[Questioner] → Uses ask_user tool for each node
    ↓
[User Responses] → "ask tomorrow", "yes here's the data", "skip this"
    ↓
[Implementer] → Applies fixes based on user responses
    ↓
Output: Improved Knowledge Graph
```

## 📁 Structure

```
kg_repair_pipeline/
├── pipeline_orchestrator.py          # Main pipeline controller
├── stages/
│   ├── analyzer.py                  # Identifies problematic nodes
│   ├── critic.py                   # Validates findings
│   ├── questioner.py               # Asks users for missing data
│   └── implementer.py              # Applies fixes to KG
├── data_models/
│   ├── problematic_node.py         # ProblematicNode data model
│   ├── user_response.py            # UserResponse data model
│   └── pipeline_state.py           # Pipeline state management
├── utils/
│   └── kg_operations.py            # KG read/write operations
├── test_pipeline.py                # Test script
└── README.md                       # This file
```

## 🚀 Usage

### Basic Usage

```python
from pipeline_orchestrator import KGPipelineOrchestrator

# Create pipeline
pipeline = KGPipelineOrchestrator()

# Run pipeline
result = pipeline.run_pipeline(kg_info={"total_nodes": 1000})

# Check results
print(f"Pipeline completed: {result.current_stage}")
print(f"Nodes resolved: {result.nodes_resolved}")
```

### Individual Stage Usage

```python
from stages.analyzer import KGAnalyzer
from stages.critic import KGCritic
from stages.questioner import KGQuestioner
from stages.implementer import KGImplementer

# Analyze KG
analyzer = KGAnalyzer()
analysis_result = analyzer.analyze_kg(kg_info)

# Critique findings
critic = KGCritic()
critique_result = critic.critique_node(problematic_node)

# Ask user questions
questioner = KGQuestioner()
user_response = questioner.ask_user_about_node(problematic_node)

# Implement fixes
implementer = KGImplementer()
implementation_result = implementer.implement_fixes(problematic_node)
```

## 📊 Data Models

### ProblematicNode

```python
class ProblematicNode(BaseModel):
    node_id: str
    problem_description: str
    node_type: Optional[str] = None
    current_attributes: Optional[dict] = None
    priority: Optional[str] = "medium"
    status: str = "identified"
    user_response: Optional[str] = None
    user_data: Optional[dict] = None
    resolution_notes: Optional[str] = None
```

### UserResponse

```python
class UserResponse(BaseModel):
    node_id: str
    response_type: ResponseType  # PROVIDE_DATA, SKIP, ASK_LATER, NO_IDEA, INVALID
    raw_response: str
    provided_data: Optional[Dict[str, Any]] = None
    ask_again_at: Optional[datetime] = None
    ask_again_in_minutes: Optional[int] = None
```

## 🔄 Pipeline Stages

### 1. Analyzer Stage
- **Purpose**: Identifies problematic nodes in the knowledge graph
- **Repurposes**: Existing `kg_explorer::analyzer::planner` agent
- **Output**: List of `ProblematicNode` objects

### 2. Critic Stage  
- **Purpose**: Validates and filters analyzer findings
- **Filters**: False positives, prioritizes real issues
- **Output**: Validated `ProblematicNode` objects

### 3. Questioner Stage
- **Purpose**: Asks users for missing data using `ask_user` tool
- **Handles**: User responses like "ask tomorrow", "skip this", "here's the data"
- **Output**: `UserResponse` objects with user input

### 4. Implementer Stage
- **Purpose**: Applies fixes to the knowledge graph
- **Processes**: Only nodes with user-provided data
- **Output**: Updated knowledge graph

## 🛠️ User Interaction

The pipeline uses the `ask_user` tool to interact with users. Users can respond with:

- **"Yes, here's the data: [information]"** → Provides missing data
- **"Skip this node"** → Skips the problematic node
- **"Ask me again tomorrow"** → Schedules for later
- **"I have no idea about this"** → Marks as unknown
- **"This isn't actually a problem"** → Marks as invalid

## 🧪 Testing

Run the test script to verify pipeline functionality:

```bash
python test_pipeline.py
```

The test script includes:
- Individual stage testing
- Full pipeline testing
- Mock data generation

## 🔧 Configuration

### Pipeline Settings

```python
pipeline_state = PipelineState(
    max_nodes_per_batch=10,      # Limit nodes processed in one run
    auto_skip_threshold=3,        # Skip nodes after N failed attempts
    max_retries=3                # Maximum retry attempts
)
```

### Stage Settings

Each stage can be configured independently:
- **Analyzer**: Analysis scope and focus areas
- **Critic**: Validation criteria and filtering rules
- **Questioner**: Question format and user interaction
- **Implementer**: Fix application and validation

## 📈 Monitoring

Get pipeline status and progress:

```python
status = pipeline.get_pipeline_status()
print(f"Current stage: {status['current_stage']}")
print(f"Progress: {status['progress']}")
print(f"Errors: {status['errors']}")
```

## 🔄 Integration

The pipeline can be integrated into the maintenance system:

```python
# In maintenance_manager.py
def run_kg_repair_pipeline(self):
    """Run the KG repair pipeline."""
    try:
        pipeline = KGPipelineOrchestrator()
        result = pipeline.run_pipeline()
        return result
    except Exception as e:
        logger.error(f"KG repair pipeline failed: {e}")
        return None
```

## 🎯 Benefits

1. **🎯 Focused Processing**: Each stage has a specific job
2. **🔄 Sequential Flow**: Natural data processing pipeline
3. **🧪 Easy Testing**: Test each stage independently
4. **📊 State Management**: Clean data flow between stages
5. **🔧 Easy Maintenance**: Modify stages without affecting others
6. **👥 User Interaction**: Leverages existing `ask_user` tool
7. **📈 Progress Tracking**: Monitor pipeline execution
8. **🛡️ Error Handling**: Robust error management

## 🚧 Future Enhancements

- **Scheduling**: Automatic retry for "ask later" responses
- **Batch Processing**: Process multiple nodes simultaneously
- **Learning**: Improve problem detection based on user feedback
- **Analytics**: Track repair success rates and patterns
- **Integration**: Connect with existing KG maintenance tools
