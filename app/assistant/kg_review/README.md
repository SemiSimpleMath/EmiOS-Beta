# KG Review System

**Unified review system for all knowledge graph findings and improvements**

## Overview

The KG Review system consolidates findings from multiple KG analysis tools into a single review queue. Instead of processing issues immediately, they're saved to a database where you can review them in bulk, add notes, and execute approved changes when ready.

## Architecture

```
┌──────────────────────────────────────────────────────┐
│              KG Analysis Sources                      │
├──────────────────────────────────────────────────────┤
│  • Repair Pipeline (analyzer + critic)               │
│  • KG Explorer (findings analysis)                   │
│  • Node Maintenance (suspicious nodes)               │
│  • Manual entries                                    │
└───────────────┬──────────────────────────────────────┘
                │
                ▼
┌──────────────────────────────────────────────────────┐
│           kg_reviews Table (Database)                 │
├──────────────────────────────────────────────────────┤
│  • node_id, problem, suggestions                     │
│  • user_notes, user_instructions                     │
│  • status (pending → approved → completed)           │
│  • source, priority, confidence                      │
└───────────────┬──────────────────────────────────────┘
                │
                ▼
┌──────────────────────────────────────────────────────┐
│         KG Review Dashboard (Web UI)                  │
├──────────────────────────────────────────────────────┤
│  • View all findings in one place                    │
│  • Add notes and instructions                        │
│  • Approve/reject reviews                            │
│  • Execute in batch                                  │
└───────────────┬──────────────────────────────────────┘
                │
                ▼
┌──────────────────────────────────────────────────────┐
│           kg_team (Implementation)                    │
├──────────────────────────────────────────────────────┤
│  Executes approved changes to the KG                 │
└──────────────────────────────────────────────────────┘
```

## Components

### 1. Database Model (`kg_review.py`)

```python
from app.assistant.kg_review.data_models.kg_review import KGReview

# Core fields:
- id (UUID)
- node_id (UUID)  
- problem_description (text)
- critic_suggestion (text)
- user_notes (text)
- user_instructions (text)
- status (pending | approved | rejected | completed)
- source (repair_pipeline | explorer | maintenance)
- priority (high | medium | low)
```

### 2. Review Manager (`review_manager.py`)

Business logic for managing reviews:

```python
from app.assistant.kg_review.review_manager import KGReviewManager

manager = KGReviewManager()

# Create a review
review = manager.create_review(
    node_id="node-uuid",
    problem_description="Missing required data",
    source="repair_pipeline",
    critic_suggestion="Add semantic_label and description"
)

# Get reviews
pending = manager.get_pending_reviews()
approved = manager.get_approved_reviews()

# Update review
manager.update_review(
    review_id="review-uuid",
    status="approved",
    user_instructions="Change label to 'Better Label'"
)

# Execute single review
result = manager.execute_review("review-uuid")

# Execute batch
results = manager.execute_batch(["id1", "id2", "id3"])
```

### 3. Web Dashboard

**Location:** `kg_review_dashboard_web.py` (root directory)

**Run:**
```bash
python kg_review_dashboard_web.py
```

**Access:** http://localhost:5002

**Features:**
- View all reviews with filtering (status, source, priority)
- Add notes and implementation instructions
- Approve/reject reviews individually or in batch
- Execute approved changes to KG
- Real-time stats dashboard

## Usage Workflows

### Workflow 1: Review Repair Pipeline Findings

```bash
# 1. Run repair pipeline (automatically saves to kg_reviews)
from app.assistant.kg_repair_pipeline.pipeline_orchestrator import KGPipelineOrchestrator

orchestrator = KGPipelineOrchestrator()  # Non-interactive by default
orchestrator.run_pipeline(max_nodes=20)

# 2. Open web dashboard
python kg_review_dashboard_web.py
# Navigate to http://localhost:5002

# 3. In the UI:
#    - Filter to "pending" + "repair_pipeline" 
#    - Review each finding
#    - Add implementation instructions
#    - Click "Approve" for fixes you want
#    - Click "Reject" for false positives

# 4. Execute all approved changes
#    - Click "Execute Approved" button
#    - All approved reviews sent to kg_team in batch
```

### Workflow 2: Programmatic Review

```python
from app.assistant.kg_review.review_manager import KGReviewManager

manager = KGReviewManager()

# Get all pending reviews
pending = manager.get_pending_reviews(limit=50)

# Review and approve some
for review in pending[:10]:
    print(f"Node: {review.node_label}")
    print(f"Problem: {review.problem_description}")
    print(f"Suggestion: {review.critic_suggestion}")
    
    # Add your instructions
    manager.update_review(
        review_id=str(review.id),
        user_instructions="Change label to 'X' and add description 'Y'",
        status="approved",
        reviewed_by="script"
    )

# Execute all approved
approved_ids = [str(r.id) for r in manager.get_approved_reviews()]
results = manager.execute_batch(approved_ids)

print(f"Executed {results['succeeded']} successfully")
print(f"Failed {results['failed']}")
```

### Workflow 3: Add Manual Review

```python
from app.assistant.kg_review.review_manager import KGReviewManager

manager = KGReviewManager()

# Create manual review
review = manager.create_review(
    node_id="some-node-uuid",
    problem_description="Label is too generic",
    source="manual",
    finding_type="quality",
    node_label="Current Label",
    critic_suggestion="Use more specific label",
    priority="high"
)

print(f"Created review: {review.id}")
```

## Integration with Repair Pipeline

The repair pipeline is already integrated! By default it saves all findings to `kg_reviews`:

```python
# In pipeline_orchestrator.py
orchestrator = KGPipelineOrchestrator(
    enable_questioning=False,   # Don't ask user - save to DB
    enable_implementation=False # Don't execute - save for later
)

# This saves all findings to kg_reviews table
orchestrator.run_pipeline()
```

## Database Schema

**Table:** `kg_reviews`

Key fields:
- `id`: UUID primary key
- `node_id`: UUID of the node to fix
- `source`: Where the finding came from
- `finding_type`: Type of problem
- `problem_description`: What's wrong
- `analyzer_suggestion`: Suggestion from analyzer
- `critic_suggestion`: Suggestion from critic
- `user_notes`: Your notes
- `user_instructions`: Instructions for kg_team
- `status`: Current status
- `priority`: Priority level
- Timestamps for tracking

**Indexes:**
- `(status, priority)` - Fast filtering
- `(source, status)` - Source-based queries  
- `(node_id, status)` - Node-based queries
- `created_at` - Chronological sorting

## API Endpoints

When running `kg_review_dashboard_web.py`:

### GET `/api/stats`
Get review statistics

### GET `/api/reviews?status=pending&source=repair_pipeline&limit=100`
Get reviews with filtering

### GET `/api/review/<id>`
Get single review

### PUT `/api/review/<id>`
Update review (notes, instructions, status)

### POST `/api/review/<id>/approve`
Approve review

### POST `/api/review/<id>/reject`
Reject review

### POST `/api/review/<id>/execute`
Execute single review

### POST `/api/batch/approve`
Approve multiple reviews
```json
{
  "review_ids": ["id1", "id2"],
  "reviewed_by": "user"
}
```

### POST `/api/batch/execute`
Execute multiple approved reviews
```json
{
  "review_ids": ["id1", "id2", "id3"]
}
```

## Status Flow

```
pending → under_review → approved → implementing → completed
                              ↓
                          rejected
```

- **pending**: Awaiting review
- **under_review**: Currently being reviewed
- **approved**: Approved for implementation
- **implementing**: Being executed by kg_team
- **completed**: Successfully implemented
- **failed**: Implementation failed
- **rejected**: Rejected (won't be implemented)
- **postponed**: Postponed for later

## Best Practices

1. **Review in batches**: Filter by source/priority and review similar items together
2. **Be specific**: Add clear implementation instructions for kg_team
3. **Use notes**: Document why you approved/rejected for future reference
4. **Test small batches**: Execute 5-10 reviews first to verify everything works
5. **Check failures**: Review failed implementations and adjust instructions
6. **Mark false positives**: Reject false positives so the system learns

## Extending the System

### Add New Source

```python
from app.assistant.kg_review.review_manager import KGReviewManager

manager = KGReviewManager()

# Your analysis code finds issues...
for issue in your_analysis_results:
    manager.create_review(
        node_id=issue.node_id,
        problem_description=issue.problem,
        source="your_new_source",  # Add your source name
        finding_type="quality",
        critic_suggestion=issue.suggested_fix,
        priority="medium"
    )
```

### Custom Filters

Modify `kg_review_dashboard_web.py` to add new filters in the UI or API.

## Troubleshooting

**Reviews not showing up?**
- Check database: `SELECT COUNT(*) FROM kg_reviews WHERE status='pending'`
- Verify repair pipeline is running in non-interactive mode
- Check logs for errors during save

**Execution failing?**
- Verify node still exists in KG
- Check implementation instructions are clear
- Review error in `implementation_error` field
- Check kg_team logs

**Web UI not loading?**
- Verify Flask is running on port 5002
- Check browser console for errors
- Verify database connection

## Future Enhancements

- [ ] Add KG Explorer integration
- [ ] Add email/Slack notifications for new reviews
- [ ] Add confidence scoring for auto-approval
- [ ] Add review templates for common fixes
- [ ] Add review history and audit log
- [ ] Add user permissions and multi-user support

## Support

For issues or questions, check:
1. This README
2. Code comments in `review_manager.py`
3. Web UI tooltips
4. Repair pipeline documentation: `kg_repair_pipeline/USAGE_EXAMPLE.md`

