# Task & Skill Architecture

## Problem

The current system has three overlapping formats for describing work:

1. **Compiled tasks** (TaskSpec JSON/IR) — produced by the task creator UI, enriched by a planner, compiled to executable IR
2. **Practice specs** (task_spec.md with YAML frontmatter) — hand-written markdown used by the playwright manager's SpecStepFeederNode
3. **No formal skill concept** — domain knowledge ("how DoorDash modals work") is embedded in individual task specs and rediscovered each time

These don't compose well. A successful Starbucks ordering run produces knowledge that can't transfer to a Pizza Hut run. The refiner improves individual task specs but there's no shared knowledge layer.

## Core Insight

**Tasks say WHAT to do. Skills say HOW to do it.**

A task is a specific request: "Order a grande iced caffe latte with oat milk and caramel syrup from Starbucks on DoorDash."

A skill is reusable operational knowledge: "How to navigate DoorDash, open item modals, customize options, handle overlay interception, and verify cart state."

The two are complementary. A task references a skill. The skill improves with every execution. New tasks on the same platform start with existing skills and don't rediscover the basics.

## Lifecycle

```
1. User creates a task
   "Order a latte from Starbucks with oat milk"

2. System finds or creates a companion skill
   - If a doordash-starbucks skill exists → load it
   - Else if a doordash-ordering skill exists → seed from it
   - Else → start with generic guidance (playwright base)

3. Task executes using the skill
   - The skill provides step-by-step guidance, tool hints, domain knowledge
   - The planner follows the skill's instructions

4. Execution trace is recorded

5. Skill learner runs
   - Analyzes the trace
   - Extracts/refines the "how" knowledge
   - Updates the skill (not the task)

6. Next run is better
   - Same task or similar task loads the improved skill
   - Fewer wasted actions, faster completion
```

## Skill Hierarchy

Skills compose hierarchically. Child skills inherit from parent skills and add specifics.

```
playwright-guide (base)
  └── doordash-ordering (platform skill)
        ├── doordash-starbucks (restaurant skill)
        ├── doordash-pizza-hut (restaurant skill)
        └── doordash-mcdonalds (restaurant skill)
```

The platform skill (`doordash-ordering`) contains knowledge common across all DoorDash restaurants:
- Search bar location and usage patterns
- Modal structure (role="dialog")
- Cart badge as the success signal
- Overlay interception workaround (web_page_coords fallback)
- Two-stage modal patterns

The restaurant skill (`doordash-starbucks`) adds restaurant-specific knowledge:
- Size selection → full customization modal flow
- Milk options are radios, syrups are quantity steppers
- Use web_modal_search for syrups (label and + button are disconnected)

A new restaurant task starts with the platform skill loaded. After its first run, the skill learner creates a restaurant-specific skill. After multiple restaurant runs, the skill learner promotes common patterns up to the platform skill.

## Skill Format

A skill is a folder with a markdown file containing YAML frontmatter and structured guidance. Aligns with the Anthropic SKILL.md format for portability.

```
skills/
  doordash-ordering/
    SKILL.md
  doordash-starbucks/
    SKILL.md
  doordash-pizza-hut/
    SKILL.md
```

### SKILL.md Structure

```markdown
---
name: doordash-starbucks
description: Order customized Starbucks drinks on DoorDash. Use when
  the user wants to order from Starbucks via DoorDash.
parent: doordash-ordering
manager: playwright_manager
version: 3
refined_from_runs: 3
last_refined: 2026-04-13T18:13:00+00:00
---

# Steps

step 1: Navigate to Starbucks and find the drink
  after: The full customization modal is open with milk/syrup options
  guidance:
    - Navigate directly to the Starbucks store URL
    - Search for the drink using web_fill_xy in the Item Search field
    - Click the drink, then select the size to enter the full modal

step 2: Customize and add to cart
  after: The cart badge shows the item count increased by 1
  guidance:
    - Use web_modal_scan to see all options
    - Click milk option by ref
    - Use web_modal_search for syrups (quantity stepper labels
      are disconnected from their + buttons)
    - Click Add to cart

# Domain Knowledge

- Starbucks drinks use a 2-stage flow: item card → size modal → full
  customization modal
- Simple substitutions (milk) are radios
- Add-ons (syrups) are quantity steppers with disconnected labels
- Use web_modal_search to find syrup items and their nearby + button refs

# Recovery

- IF overlay intercepts a click: use web_page_coords + web_click_xy_snapshot
- IF cart already has items: success = cart count increased by 1, not
  exactly "1 items"
```

## Task Format

A task is simple — just the what. It references a skill and provides parameters.

```markdown
---
task_id: order-starbucks-latte
skill: doordash-starbucks
---

# Goal
Order a grande iced caffe latte with oat milk and caramel syrup from
Starbucks on DoorDash.

# Parameters
- drink: Iced Caffe Latte
- size: Grande
- milk: Oatmilk
- additions: Caramel Syrup (1 pump)

# Success
Cart badge shows item count increased by 1.
```

## Workflow (Optional Compilation)

For complex multi-step tasks, the user can compile a task into a workflow IR. Each step in the workflow references a skill.

```json
{
  "schema_version": "workflow_v1",
  "task_id": "lunch-order-and-notify",
  "steps": [
    {
      "id": "step_1",
      "kind": "action",
      "title": "Order lunch",
      "skill": "doordash-ordering",
      "executor": "playwright_manager",
      "instruction": "Order yellow curry chicken with potato and steamed rice from Thai Spice",
      "next_step": "step_2"
    },
    {
      "id": "step_2",
      "kind": "action",
      "title": "Notify Jamie",
      "skill": "sms-send",
      "executor": "emi_team_manager",
      "instruction": "Text Jamie: food is on the way",
      "next_step": "step_3"
    },
    {
      "id": "step_3",
      "kind": "wait",
      "title": "Wait for delivery",
      "wait_type": "duration",
      "duration_minutes": 30,
      "next_step": "step_4"
    },
    {
      "id": "step_4",
      "kind": "end",
      "title": "Done"
    }
  ]
}
```

## UI Changes

The task creation UI gets two panels:

1. **Task panel** — what to do: title, goal, parameters. Quick and simple.
2. **Skill panel** — how to do it: the current "plan" phase becomes skill creation/editing. Shows step guidance, tool hints, domain knowledge. Can load an existing skill or create a new one.
3. **Compile** (optional) — for multi-step workflows. Produces executable IR.

## System Changes

### New Components

- **Skill registry** — discovers and loads skills from `skills/` directory. Matches skills to tasks by name or description.
- **Skill learner** — replaces the current task refiner. Runs after execution, updates the skill, not the task. Cross-task learning promotes common patterns to parent skills.

### Modified Components

- **SpecStepFeederNode** — reads skill content (steps, guidance, domain knowledge) instead of practice spec format. Feeds steps to the planner.
- **Task creator UI** — "plan" phase becomes skill creation. Skill panel shows guidance and domain knowledge.
- **Practice runner** — after execution, calls the skill learner instead of the task refiner.

### Unchanged Components

- **Compiler** — still produces workflow IR. Steps now reference skills.
- **Planner agent** — still receives step-by-step instructions. Doesn't know about skills directly — the feeder node resolves skills into instructions.
- **Execution trace recorder** — unchanged. Traces are the input to the skill learner.

## Migration Path

1. Extract skills from existing practice specs (Starbucks, Pizza Hut, McDonald's)
2. Create the `skills/` directory and skill registry
3. Build the skill learner from the existing refiner + analyst agents
4. Update SpecStepFeederNode to load skills
5. Update the task creator UI to show the skill panel
6. Cross-task skill learning (platform skills) as a follow-on

## Open Questions

- Should skills be versioned with full history, or just latest?
- How does the skill learner decide when to promote patterns to a parent skill? After N traces? Manual trigger?
- Should skills be publishable/shareable (Anthropic SKILL.md format) or internal-only?
- How do parameters flow from the task into the skill's step guidance? Template variables? String substitution?
