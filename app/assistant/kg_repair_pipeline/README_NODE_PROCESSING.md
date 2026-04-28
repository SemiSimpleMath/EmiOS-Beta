# Node Processing Tracking System

## Overview

The Node Processing Tracking System provides comprehensive tracking and management of nodes in the KG repair pipeline. It maintains a persistent database of node processing status, user interactions, scheduling, and learning from user feedback.

## Key Features

### 🎯 **Node Status Tracking**
- **Comprehensive Status Management**: Track nodes through their entire lifecycle
- **Priority-Based Processing**: Handle critical, high, medium, and low priority nodes
- **False Positive Learning**: Learn from user feedback to avoid re-processing invalid problems
- **Scheduling System**: Schedule nodes for later review based on user preferences

### 📊 **User Interaction Management**
- **Natural Language Response Parsing**: Intelligently classify user responses
- **Data Extraction**: Extract structured data from natural language responses
- **Confidence Tracking**: Track user confidence in their responses
- **Response Type Classification**: Categorize responses (provide_data, skip, ask_later, etc.)

### 🔄 **Pipeline Integration**
- **Seamless Integration**: Works with existing pipeline stages
- **Status Persistence**: Maintains state across pipeline runs
- **Batch Processing**: Handle multiple nodes efficiently
- **Error Recovery**: Graceful handling of processing errors

## Database Schema

### NodeProcessingStatus Table

| Field | Type | Description |
|-------|------|-------------|
| `id` | UUID | Primary key |
| `node_id` | UUID | Reference to KG node |
| `node_label` | String | Cached node label |
| `node_type` | String | Cached node type |
| `status` | String | Current processing status |
| `priority` | String | Priority level (critical, high, medium, low) |
| `problem_description` | Text | Description of identified problem |
| `problem_category` | String | Category of problem |
| `problem_severity` | String | Severity level |
| `analyzer_suggestion` | Text | Analyzer's suggestion |
| `critic_validation` | Text | Critic's validation |
| `critic_suggestions` | Text | Actionable suggestions |
| `user_response` | Text | Raw user response |
| `user_response_type` | String | Type of user response |
| `user_provided_data` | JSONB | Structured data from user |
| `user_confidence` | Float | User's confidence (0.0-1.0) |
| `implementation_instructions` | JSONB | Generated instructions |
| `implementation_status` | String | Implementation status |
| `implementation_result` | Text | Implementation result |
| `first_identified_at` | DateTime | When first identified |
| `last_offered_at` | DateTime | When last offered to user |
| `next_review_at` | DateTime | When to offer again |
| `resolved_at` | DateTime | When resolved |
| `attempt_count` | Integer | Number of attempts |
| `max_attempts` | Integer | Maximum attempts |
| `is_false_positive` | Boolean | Was this a false positive? |
| `user_marked_invalid` | Boolean | User said not a problem |
| `should_skip_future` | Boolean | Skip in future analysis |
| `learning_notes` | Text | Notes for learning |
| `pipeline_id` | String | Pipeline run ID |
| `batch_id` | String | Batch ID |
| `processing_agent` | String | Processing agent |
| `tags` | JSONB | Flexible tagging |
| `notes` | Text | General notes |
| `metadata` | JSONB | Additional metadata |

## Usage Examples

### 1. Creating Node Processing Status

```python
from app.assistant.kg_repair_pipeline.utils.node_processing_manager import NodeProcessingManager

manager = NodeProcessingManager()

# Create processing status for a problematic node
status = manager.create_node_status(
    node_id="node_123",
    node_label="Wedding Event",
    node_type="event",
    problem_description="Missing start_date and end_date",
    priority="high"
)
```

### 2. Recording User Responses

```python
# Record user response with data extraction
success = manager.record_user_response(
    node_id="node_123",
    user_response="The wedding was on June 15, 2023 at St. Mary's Church",
    response_type="provide_data",
    provided_data={
        "start_date": "2023-06-15",
        "description": "wedding at St. Mary's Church"
    },
    confidence=0.9
)
```

### 3. Scheduling Nodes for Later

```python
# Schedule node for later review
from datetime import datetime, timezone, timedelta

schedule_time = datetime.now(timezone.utc) + timedelta(hours=24)
success = manager.schedule_node_for_later(
    node_id="node_123",
    schedule_time=schedule_time
)
```

### 4. Getting Nodes for Processing

```python
# Get nodes that need processing
nodes = manager.get_nodes_for_processing(max_nodes=10, priority_order=True)

for node in nodes:
    print(f"Node: {node.node_label}, Status: {node.status}, Priority: {node.priority}")
```

### 5. Handling Different Response Types

```python
# Handle user response types
if user_response.response_type == ResponseType.ASK_LATER:
    # Schedule for later
    manager.schedule_node_for_later(node_id, schedule_time)
    
elif user_response.response_type == ResponseType.SKIP:
    # Mark as skipped
    manager.mark_node_as_skipped(node_id, "User requested to skip")
    
elif user_response.response_type == ResponseType.INVALID:
    # Mark as invalid (not a problem)
    manager.mark_node_as_invalid(node_id, "User marked as not a problem")
```

## Response Type Classification

The system intelligently classifies user responses into structured types:

### Response Types

1. **PROVIDE_DATA**: User provides missing information
   - *"The wedding was on June 15, 2023"*
   - *"Here's the data: start_date: 2023-06-15"*

2. **SKIP**: User wants to skip this node
   - *"Skip this one"*
   - *"Not important"*

3. **ASK_LATER**: User wants to be asked later
   - *"Ask me again tomorrow"*
   - *"Remind me in a week"*

4. **NO_IDEA**: User doesn't know
   - *"I don't know"*
   - *"No idea"*

5. **INVALID**: User says this isn't a problem
   - *"This is fine as is"*
   - *"Not a problem"*

### Data Extraction

The system extracts structured data from natural language:

```python
# Example user response: "The wedding was on June 15, 2023 at St. Mary's Church"
extracted_data = {
    "start_date": "2023-06-15",
    "description": "wedding at St. Mary's Church",
    "raw_data": "The wedding was on June 15, 2023 at St. Mary's Church",
    "extracted_at": "2025-01-27T10:30:00Z"
}
```

## Pipeline Integration

### Analysis Stage
- Creates processing status records for problematic nodes
- Marks non-problematic nodes as invalid to avoid re-processing

### Questioning Stage
- Records user responses and response types
- Handles scheduling based on user preferences
- Updates node status based on user feedback

### Implementation Stage
- Updates implementation status and results
- Records success/failure of implementation

## Learning and Improvement

### False Positive Learning
- Tracks nodes marked as invalid by users
- Prevents re-processing of known false positives
- Improves future analysis accuracy

### User Preference Learning
- Learns from user scheduling preferences
- Adapts to user availability patterns
- Improves user experience over time

### Performance Tracking
- Monitors processing success rates
- Tracks user satisfaction metrics
- Identifies areas for improvement

## Statistics and Analytics

### Processing Statistics
```python
stats = manager.get_processing_statistics(days=30)
# Returns:
{
    'total_nodes': 150,
    'resolved_nodes': 120,
    'skipped_nodes': 20,
    'invalid_nodes': 10,
    'resolution_rate': 0.8,
    'false_positive_rate': 0.067
}
```

### Batch Processing
- Track processing batches
- Monitor batch completion rates
- Identify bottlenecks and issues

## Best Practices

### 1. **Node Selection Priority**
1. Scheduled nodes (ready for review)
2. New/unprocessed nodes
3. Random existing nodes

### 2. **User Interaction Handling**
- Always record user responses
- Handle different response types appropriately
- Respect user scheduling preferences

### 3. **Learning Integration**
- Mark false positives to avoid re-processing
- Learn from user feedback patterns
- Adapt processing strategies

### 4. **Error Handling**
- Graceful handling of processing errors
- Retry mechanisms for failed operations
- Comprehensive error logging

## Testing

Run the test script to verify the system:

```bash
python app/assistant/kg_repair_pipeline/test_node_processing.py
```

This will test:
- Node status creation and updates
- User response recording
- Scheduling system
- Statistics generation
- Error handling

## Future Enhancements

### Planned Features
- **Machine Learning Integration**: Use ML to improve problem detection
- **Advanced Scheduling**: Smart scheduling based on user patterns
- **Collaborative Processing**: Multiple users working on nodes
- **Advanced Analytics**: Detailed performance metrics and insights
- **Integration APIs**: REST APIs for external system integration

### Scalability Considerations
- **Database Optimization**: Index optimization for large datasets
- **Caching**: Redis integration for frequently accessed data
- **Async Processing**: Background processing for large batches
- **Distributed Processing**: Multi-instance processing support
