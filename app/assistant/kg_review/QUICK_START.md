# KG Review System - Quick Start

## 🚀 Get Started in 3 Steps

### Step 1: Create the Database Table

```bash
cd c:\Users\semis\IdeaProjects\EmiAi
python
```

```python
from app.models.base import Base, engine
from app.assistant.kg_review.data_models.kg_review import KGReview

# Create the table
Base.metadata.create_all(engine, tables=[KGReview.__table__])
print("✅ Created kg_reviews table")
exit()
```

### Step 2: Generate Some Reviews

Run the repair pipeline to generate reviews:

```bash
python
```

```python
import app.assistant.tests.test_setup  # Initialize

from app.assistant.kg_repair_pipeline.pipeline_orchestrator import KGPipelineOrchestrator

# Run pipeline (saves to kg_reviews automatically)
orchestrator = KGPipelineOrchestrator()  # Non-interactive by default
result = orchestrator.run_pipeline(max_nodes=10)

print(f"✅ Generated reviews for {result.nodes_validated} nodes")
exit()
```

### Step 3: Review and Execute

Start the web dashboard:

```bash
python kg_review_dashboard_web.py
```

Then open your browser to: **http://localhost:5002**

## 📝 Using the Dashboard

1. **Filter reviews**: Use dropdowns to filter by status/source/priority
2. **Add instructions**: Write implementation instructions in the textarea
3. **Approve**: Click "✓ Approve" on reviews you want to implement
4. **Execute**: Click "⚡ Execute Approved" to apply all approved changes

## 🔄 Typical Workflow

```
┌─────────────┐
│ Run Pipeline│  ← Generates 20 reviews
│  max_nodes=20│
└──────┬──────┘
       │
       ▼
┌─────────────┐
│ Open Web UI │  ← Review at http://localhost:5002
│  port :5002  │
└──────┬──────┘
       │
       ▼
┌─────────────┐
│Filter pending│  ← See all pending reviews
│   reviews    │
└──────┬──────┘
       │
       ▼
┌─────────────┐
│ For each:   │
│ 1. Read problem│
│ 2. Add instructions│
│ 3. Approve │
└──────┬──────┘
       │
       ▼
┌─────────────┐
│   Execute   │  ← Click "Execute Approved"
│   Approved  │    Sends to kg_team
└─────────────┘
```

## 💡 Tips

- **Start small**: Approve 5-10 reviews first to test
- **Be specific**: Write clear instructions like "Change label to 'X'"
- **Check results**: Review completed items to verify success
- **Reject false positives**: Help the system learn

## 🎯 Example Instructions

Good instructions for kg_team:

✅ "Change label to 'Movie Watching' and add semantic_label 'Movie Watching (Jukka)'"

✅ "Update description to 'Jukka's ongoing habit of watching movies'"

✅ "Add category 'leisure_activity' and type 'State'"

❌ "Fix it" (too vague)

❌ "Make it better" (not actionable)

## 📊 Check Status Programmatically

```python
from app.assistant.kg_review.review_manager import KGReviewManager

manager = KGReviewManager()

# Get stats
stats = manager.get_stats()
print(f"Total: {stats['total']}")
print(f"Pending: {stats['by_status'].get('pending', 0)}")
print(f"Completed: {stats['by_status'].get('completed', 0)}")

# Get pending reviews
pending = manager.get_pending_reviews(limit=10)
for review in pending:
    print(f"- {review.node_label}: {review.problem_description}")
```

## 🐛 Troubleshooting

**No reviews showing?**
```python
# Check if reviews were created
from app.models.base import get_session
from app.assistant.kg_review.data_models.kg_review import KGReview

session = get_session()
count = session.query(KGReview).count()
print(f"Total reviews in database: {count}")
```

**Web UI won't start?**
- Make sure port 5002 is not in use
- Check that Flask is installed: `pip install flask`
- Verify test_setup.py doesn't have errors

**Execution fails?**
- Check that node still exists in KG
- Verify instructions are clear and specific
- Check `implementation_error` field for details

## 📚 Next Steps

- Read full documentation: `app/assistant/kg_review/README.md`
- Explore the codebase: `app/assistant/kg_review/`
- Check repair pipeline docs: `app/assistant/kg_repair_pipeline/USAGE_EXAMPLE.md`

## ✨ That's it!

You now have a unified review system for all KG improvements. No more interruptions during pipeline runs - review everything at your convenience!

