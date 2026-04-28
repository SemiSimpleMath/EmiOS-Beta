"""
Seed a large, personalized taxonomy with thousands of types.

This creates a comprehensive hierarchical taxonomy covering:
- Activities (10,000+ combinations)
- Skills (programming languages, tools, ML tasks)
- Education (academic fields, degrees, courses)
- Health (conditions, symptoms, medications)
- Creative works (media types, genres)
- Events (professional, social, travel, milestones)
- States (relationships, employment, financial)
- Entities (organizations, locations, possessions)
- Goals (personal, professional, project)
- And much more...

All taxonomy labels follow lowercase_snake_case convention.
"""

import sys
from itertools import product
from textwrap import dedent

from app.models.base import get_session
from app.assistant.kg_core.taxonomy.models import Taxonomy
from app.assistant.kg_core.kg_utils.knowledge_graph_utils import KnowledgeGraphUtils


PRINT_EVERY = 500  # throttle console prints


def main():
    print("🌱 Seeding large, personalized taxonomy...")
    print("⚠️  This will create thousands of taxonomy types - may take several minutes!")
    session = get_session()
    kg = KnowledgeGraphUtils(session)

    try:
        sentinel = session.query(Taxonomy).filter_by(label="large_personalized_taxonomy_seeded").first()
        if sentinel:
            print("✅ Large personalized taxonomy already seeded. Nothing to do.")
            sys.exit(0)

        roots = ensure_roots(session, kg)
        backbone_ids = seed_backbone(session, kg, roots)
        total_created = 0

        # Seeding blocks
        print("\n📦 Seeding activity combinations...")
        total_created += seed_activity_block(session, kg, backbone_ids)
        print(f"   Created {total_created} activity types")
        
        print("\n📦 Seeding skills...")
        total_created += seed_skill_block(session, kg, backbone_ids)
        total_created += seed_soft_and_practical_skills_block(session, kg, backbone_ids)
        
        print("\n📦 Seeding education...")
        total_created += seed_education_block(session, kg, backbone_ids)
        total_created += seed_university_life_block(session, kg, backbone_ids)
        
        print("\n📦 Seeding health...")
        total_created += seed_health_block(session, kg, backbone_ids)
        total_created += seed_mental_health_and_wellness_block(session, kg, backbone_ids)
        
        print("\n📦 Seeding creative works and games...")
        total_created += seed_creative_work_block(session, kg, backbone_ids)
        total_created += seed_games_block(session, kg, backbone_ids)
        
        print("\n📦 Seeding events...")
        total_created += seed_event_blocks(session, kg, backbone_ids)
        total_created += seed_scheduled_and_ceremonial_events_block(session, kg, backbone_ids)
        total_created += seed_routine_activities_block(session, kg, backbone_ids)
        
        print("\n📦 Seeding states...")
        total_created += seed_state_blocks(session, kg, backbone_ids)
        total_created += seed_personal_states_block(session, kg, backbone_ids)
        total_created += seed_detailed_states_block(session, kg, backbone_ids)
        total_created += seed_rituals_block(session, kg, backbone_ids)
        
        print("\n📦 Seeding entities...")
        total_created += seed_entity_blocks(session, kg, backbone_ids)
        total_created += seed_detailed_entities_block(session, kg, backbone_ids)
        total_created += seed_family_and_social_block(session, kg, backbone_ids)
        total_created += seed_person_roles_block(session, kg, backbone_ids)
        
        print("\n📦 Seeding life milestones, finance, identity...")
        total_created += seed_life_milestones_block(session, kg, backbone_ids)
        total_created += seed_finance_and_home_block(session, kg, backbone_ids)
        total_created += seed_identity_block(session, kg, backbone_ids)
        
        print("\n📦 Seeding goals and projects...")
        total_created += seed_goals_block(session, kg, backbone_ids)
        total_created += seed_projects_block(session, kg, backbone_ids)
        
        print("\n📦 Seeding additional concepts...")
        total_created += seed_additional_concepts_block(session, kg, backbone_ids)

        add_type(session, kg, "concept", "large_personalized_taxonomy_seeded", "Marker for large personalized taxonomy", prints=False)
        session.commit()

        total = session.query(Taxonomy).count()
        print("\n🎉 Done!")
        print(f"   Created ~{total_created} new taxonomy types in this run.")
        print(f"   Total taxonomy rows now: {total}")

        print(dedent("""
        💡 Structure highlights (abbrev.):
           entity → person, organization, location, possession, ai_agent, animal, pet, family_group, social_group, project
           event → professional_event, social_event, travel, educational_event, health_event, life_milestone, routine_activity
           state → relationship_state, employment, ownership, financial_state, ritual
           goal → personal_goal, professional_goal, project_goal
           concept → activity, skill, educational_concept, health_concept, creative_work, financial_concept, legal_concept
           property → attribute, preference, interest, personal_trait, personal_value
        """))

    except Exception as e:
        print(f"❌ Error seeding taxonomy: {e}")
        import traceback
        traceback.print_exc()
        session.rollback()
        sys.exit(1)
    finally:
        session.close()


# ---------- Helpers ----------

def ensure_roots(session, kg):
    root_labels = [
        ("entity", "People, places, things, organizations, AI agents, groups"),
        ("event", "Occurrences, activities, meetings, milestones"),
        ("state", "Conditions, statuses, relationships over time"),
        ("goal", "Objectives, plans, and desired outcomes"),
        ("concept", "Abstract ideas, categories, and knowledge domains"),
        ("property", "Characteristics, qualities, and personal attributes"),
    ]
    ids = {}
    for label, desc in root_labels:
        ids[label] = get_or_create(session, kg, label, desc, parent_id=None)
    session.commit()
    return ids


def seed_backbone(session, kg, roots):
    ids = {}
    # entity
    ids["person"] = get_or_create(session, kg, "person", "Individual human beings", roots["entity"])
    ids["organization"] = get_or_create(session, kg, "organization", "Companies and institutions", roots["entity"])
    ids["location"] = get_or_create(session, kg, "location", "Places and locations", roots["entity"])
    ids["possession"] = get_or_create(session, kg, "possession", "Things owned", roots["entity"])
    ids["ai_agent"] = get_or_create(session, kg, "ai_agent", "AI assistants and systems", roots["entity"])
    ids["assistant"] = get_or_create(session, kg, "assistant", "AI assistant agents", ids["ai_agent"])
    ids["animal"] = get_or_create(session, kg, "animal", "Animals", roots["entity"])
    ids["pet"] = get_or_create(session, kg, "pet", "Domesticated animals", ids["animal"])
    ids["family_group"] = get_or_create(session, kg, "family_group", "Family units and structures", roots["entity"])
    ids["social_group"] = get_or_create(session, kg, "social_group", "Social circles and communities", roots["entity"])
    ids["creative_work_entity"] = get_or_create(session, kg, "creative_work", "TV shows, movies, books, music (as entities)", roots["entity"])
    ids["tv_show"] = get_or_create(session, kg, "tv_show", "Television shows and series", ids["creative_work_entity"])
    ids["movie"] = get_or_create(session, kg, "movie", "Films and movies", ids["creative_work_entity"])

    # event
    ids["communication"] = get_or_create(session, kg, "communication", "Communication and interaction events", roots["event"])
    ids["conversation"] = get_or_create(session, kg, "conversation", "Conversations and dialogues", ids["communication"])
    ids["discussion"] = get_or_create(session, kg, "discussion", "Discussions and debates", ids["communication"])
    ids["professional_event"] = get_or_create(session, kg, "professional_event", "Work related events", roots["event"])
    ids["social_event"] = get_or_create(session, kg, "social_event", "Social gatherings", roots["event"])
    ids["travel"] = get_or_create(session, kg, "travel", "Travel and trips", roots["event"])
    ids["educational_event"] = get_or_create(session, kg, "educational_event", "Education related events", roots["event"])
    ids["health_event"] = get_or_create(session, kg, "health_event", "Health related events", roots["event"])
    ids["life_milestone"] = get_or_create(session, kg, "life_milestone", "Major life milestones and transitions", roots["event"])

    # state
    ids["relationship_state"] = get_or_create(session, kg, "relationship_state", "Ongoing relationships", roots["state"])
    ids["employment"] = get_or_create(session, kg, "employment", "Work and employment status", roots["state"])
    ids["ownership"] = get_or_create(session, kg, "ownership", "Possession and ownership", roots["state"])
    ids["financial_state"] = get_or_create(session, kg, "financial_state", "Financial conditions and statuses", roots["state"])

    # concept
    ids["activity"] = get_or_create(session, kg, "activity", "Activities and actions", roots["concept"])
    ids["skill"] = get_or_create(session, kg, "skill", "Abilities and competencies", roots["concept"])
    ids["educational_concept"] = get_or_create(session, kg, "educational_concept", "Education concepts", roots["concept"])
    ids["health_concept"] = get_or_create(session, kg, "health_concept", "Health concepts", roots["concept"])
    ids["creative_work"] = get_or_create(session, kg, "creative_work", "Creative works and media", roots["concept"])
    ids["financial_concept"] = get_or_create(session, kg, "financial_concept", "Concepts related to finance and economy", roots["concept"])
    ids["legal_concept"] = get_or_create(session, kg, "legal_concept", "High-level legal concepts", roots["concept"])

    # property
    ids["attribute"] = get_or_create(session, kg, "attribute", "Characteristics and qualities", roots["property"])
    ids["preference"] = get_or_create(session, kg, "preference", "Likes and dislikes", ids["attribute"])
    ids["interest"] = get_or_create(session, kg, "interest", "Areas of interest", ids["attribute"])
    ids["personal_trait"] = get_or_create(session, kg, "personal_trait", "Personality traits", ids["attribute"])
    ids["personal_value"] = get_or_create(session, kg, "personal_value", "Personal values and beliefs", roots["property"])

    # goals (simple backbone)
    ids["personal_goal"] = get_or_create(session, kg, "personal_goal", "Personal goals and objectives", roots["goal"])
    ids["professional_goal"] = get_or_create(session, kg, "professional_goal", "Professional goals and objectives", roots["goal"])

    session.commit()
    return ids


def add_type(session, kg, parent_label, label, description, prints=False):
    parent = session.query(Taxonomy).filter_by(label=parent_label).first()
    if not parent:
        raise ValueError(f"Parent label not found: {parent_label}")
    if session.query(Taxonomy).filter_by(label=label).first():
        return 0
    tax = Taxonomy(
        label=label,
        description=description,
        parent_id=parent.id,
        label_embedding=kg.create_embedding(label),
    )
    session.add(tax)
    if prints:
        print(f"  ✅ {parent_label} → {label}")
    return 1


def get_or_create(session, kg, label, description, parent_id=None):
    row = session.query(Taxonomy).filter_by(label=label).first()
    if row:
        return row.id
    row = Taxonomy(
        label=label,
        description=description,
        parent_id=parent_id,
        label_embedding=kg.create_embedding(label),
    )
    session.add(row)
    session.flush()
    return row.id


def get_root_id(session, root_label):
    """Get the ID of a root taxonomy node (entity, event, state, goal, concept, property)."""
    root = session.query(Taxonomy).filter_by(label=root_label).first()
    if not root:
        raise ValueError(f"Root node '{root_label}' not found - ensure roots are seeded first")
    return root.id


# ---------- Blocks ----------

def seed_activity_block(session, kg, ids):
    verbs_master = [
        "run","walk","cycle","swim","row","hike","climb","lift","yoga","dance","box","spar","kayak","camp","backpack",
        "code","debug","design","model","analyze","simulate","prototype","test","review","document","refactor","deploy",
        "cook","bake","grill","brew","garden","paint","draw","sculpt","compose","sing","play_instrument","write_music",
        "read","write","edit","present","teach","learn","mentor","coach","organize","plan","negotiate","research","theorize",
        "travel","photograph","videograph","podcast","blog","volunteer","meditate","drive","commute","parent","invest"
    ]
    domains_master = [
        "endurance","strength","mobility","martial","team_sport","solo_sport","water_sport","mountain_sport",
        "software","data","ml","robotics","electronics","cloud","quantitative_finance","academic_research","mathematics",
        "culinary","horticulture","visual_art","music","literature","education","philosophy","history",
        "product","project","ops","sales","marketing","finance","legal","non_profit",
        "wellness","mindfulness","outdoors","indoor","creative","communication","leadership","management","craft","maker",
        "home_improvement","vehicle_maintenance","personal_finance","parenting"
    ]
    created = 0
    parent = "activity"
    for v in verbs_master:
        created += add_type(session, kg, parent, f"activity_{v}", f"Activity verb: {v}")
    session.commit()
    cnt = 0
    for v, d in product(verbs_master, domains_master):
        created += add_type(session, kg, parent, f"activity_{v}_{d}", f"{v} activity in {d} domain")
        cnt += 1
        if cnt % PRINT_EVERY == 0:
            session.commit()
    session.commit()
    return created


def seed_skill_block(session, kg, ids):
    skill_cats = [
        "programming","data_engineering","data_science","machine_learning","statistics","mathematics","visualization",
        "devops","cloud_architecture","security","product_management","project_management","ui_ux","research",
        "writing","communication","pedagogy","law"
    ]
    generic_topics = [
        "foundations","algorithms","data_structures","testing","optimization","parallel","performance","scalability",
        "pipelines","orchestration","monitoring","governance","privacy","ethics","risk","compliance",
        "requirements","roadmapping","prioritization","estimation","documentation","formal_methods","theorem_proving",
        "prototyping","experimentation","ab_testing","dashboards","reporting","stakeholder_management","hiring","mentoring",
        "grant_writing","peer_review","curriculum_design","lecturing","advising"
    ]
    prog_langs = [
        "python","r","java","javascript","typescript","c","cpp","go","rust","scala","kotlin","swift","php","ruby",
        "matlab","julia","sql","bash","powershell","fortran","haskell","ocaml","erlang","elixir","dart","lua","perl",
        "objectivec","groovy","fsharp","nim","zig","clojure","prolog","apl","sas","stata","cobol","vbnet","solidity","tsx","jsx","tex",
        "lisp","scheme","coq","isabelle","lean"
    ]
    ds_tools = [
        "pandas","numpy","polars","arrow","dask","spark","duckdb","trino","hive","flink","airflow","prefect","dbt",
        "mlflow","ray","faiss","annoy","milvus","weaviate","qdrant","pytorch","tensorflow","keras","jax","scikitlearn",
        "langchain","llama_index","transformers","matplotlib","seaborn","plotly"
    ]
    db_systems = [
        "postgresql","mysql","sqlite","oracle_db","sqlserver","mariadb","mongodb","cassandra","redis","elasticsearch",
        "neo4j","arango","janusgraph","couchdb","dynamodb","cosmosdb","firestore","influxdb","timescaledb","opensearch","clickhouse","duckdb"
    ]
    ml_tasks = [
        "classification","regression","clustering","dimensionality_reduction","recommendation",
        "forecasting","anomaly_detection","ranking","reinforcement_learning","few_shot","fine_tuning",
        "prompt_engineering","retrieval","reranking","generation","summarization","translation","qa",
        "entity_recognition","link_prediction","graph_embedding","segmentation","detection","pose_estimation"
    ]
    created = 0
    for cat in skill_cats:
        created += add_type(session, kg, "skill", f"skill_{cat}", f"Skill category: {cat}")
    session.commit()
    cnt = 0
    for cat, topic in product(skill_cats, generic_topics):
        created += add_type(session, kg, "skill", f"skill_{cat}_{topic}", f"{cat} skill topic: {topic}")
        cnt += 1
        if cnt % PRINT_EVERY == 0:
            session.commit()
    session.commit()
    for lang in prog_langs:
        created += add_type(session, kg, "skill", f"skill_programming_language_{lang}", f"Programming language: {lang}")
    session.commit()
    for tool in ds_tools:
        created += add_type(session, kg, "skill", f"skill_data_tool_{tool}", f"Data tool: {tool}")
    session.commit()
    for db in db_systems:
        created += add_type(session, kg, "skill", f"skill_database_{db}", f"Database system: {db}")
    session.commit()
    for t in ml_tasks:
        created += add_type(session, kg, "skill", f"skill_ml_{t}", f"Machine learning task: {t}")
    session.commit()
    return created


def seed_soft_and_practical_skills_block(session, kg, ids):
    """Seeds a large number of soft, creative, and practical life skills."""
    print("   Sub-seeding soft and practical skills...")
    created = 0

    def build_hierarchy(parent_label, data):
        nonlocal created
        parent_node = session.query(Taxonomy).filter_by(label=parent_label).first()
        for group_label, items in data.items():
            get_or_create(session, kg, group_label, f"Skill category for {group_label.replace('_', ' ')}", parent_node.id)
            session.commit()
            if isinstance(items, dict):
                build_hierarchy(group_label, items)
            elif isinstance(items, list):
                for skill_label in items:
                    created += add_type(session, kg, group_label, skill_label, f"The skill of {skill_label.replace('_', ' ')}")
                session.commit()

    skill_hierarchy = {
        "interpersonal_skill": {
            "leadership_and_management": ["leadership", "coaching", "facilitation"],
            "communication_and_influence": ["negotiation", "conflict_resolution", "public_speaking", "presentation", "diplomacy", "persuasion"],
            "social_and_emotional": ["emotional_intelligence", "networking", "relationship_building"]
        },
        "cognitive_and_personal_effectiveness_skill": {
            "thinking_and_problem_solving": ["critical_thinking", "problem_solving", "creative_thinking", "systems_thinking"],
            "execution_and_organization": ["time_management", "organization", "prioritization", "decision_making"]
        },
        "creative_and_media_skill": {
            "visual_media": ["photography", "videography", "video_editing", "audio_editing"],
            "design_and_art": ["graphic_design", "illustration", "animation", "3d_modeling"],
            "game_development": ["game_design", "level_design", "narrative_design", "sound_design"]
        },
        "language_and_writing_skill": {
            "language_proficiency": ["language_learning", "translation", "interpretation", "linguistics"],
            "professional_writing": ["public_relations", "journalism", "copywriting", "content_strategy"]
        },
        "business_and_marketing_skill": {
            "digital_marketing": ["seo", "sem", "social_media_marketing", "email_marketing"],
            "sales_and_client_relations": ["sales", "customer_service", "account_management", "business_development"]
        },
        "financial_skill": ["budgeting", "financial_planning", "investing", "accounting"],
        "caregiving_and_health_skill": {
            "human_care": ["parenting", "childcare", "eldercare"],
            "animal_care": ["pet_training"],
            "health_and_wellness": ["therapy", "counseling", "social_work", "healthcare"]
        },
        "culinary_skill": ["cooking", "baking", "mixology", "nutrition", "meal_planning"],
        "trade_and_craft_skill": {
            "home_trades": ["carpentry", "plumbing", "electrical", "hvac"],
            "automotive_repair": ["auto_maintenance", "auto_diagnostics"],
            "handicrafts": ["sewing", "knitting", "crafting", "woodworking", "metalworking"]
        },
        "horticulture_skill": ["gardening", "landscaping", "permaculture", "botany"]
    }
    build_hierarchy("skill", skill_hierarchy)
    return created


def seed_education_block(session, kg, ids):
    academic_fields = [
        "computer_science","data_science","statistics","mathematics","pure_mathematics","applied_mathematics","logic",
        "category_theory","type_theory","set_theory","algebra","geometry","topology","analysis","number_theory",
        "physics","chemistry","biology","neuroscience","psychology","economics","finance","accounting","law",
        "political_science","sociology","philosophy","linguistics","history","literature","education",
        "electrical_engineering","mechanical_engineering","civil_engineering","industrial_engineering",
        "biomedical_engineering","aerospace_engineering","materials_science","environmental_science",
        "geology","geography","oceanography","astronomy","art_history","musicology","theater_studies",
        "film_studies","architecture","urban_planning","public_health","epidemiology","nutrition","kinesiology",
        "anthropology","communications","media_studies","design","human_computer_interaction","information_systems",
        "library_science","business_administration","marketing","management","operations_research","supply_chain",
        "agriculture","forestry","veterinary_science","medicine","nursing","dentistry","pharmacy","education_policy",
        "game_design","robotics","cognitive_science","public_policy","international_relations","classics"
    ]
    degree_kinds = ["certificate","associate","bachelor","master","phd","professional","postdoc"]
    created = 0
    for field in academic_fields:
        created += add_type(session, kg, "educational_concept", f"academic_field_{field}", f"Academic field: {field}")
    session.commit()
    for field in academic_fields:
        for deg in degree_kinds:
            created += add_type(session, kg, "educational_concept", f"degree_{deg}_{field}", f"{deg} degree in {field}")
    session.commit()
    for field in academic_fields:
        created += add_type(session, kg, "educational_concept", f"course_{field}", f"Course in {field}")
    session.commit()
    law_concepts = ["civil_law", "criminal_law", "corporate_law", "international_law", "intellectual_property", "contract_law"]
    for lc in law_concepts:
        created += add_type(session, kg, "legal_concept", f"legal_area_{lc}", f"Area of law: {lc}")
    session.commit()
    return created


def seed_health_block(session, kg, ids):
    created = 0
    cond_families = [
        "cardiovascular","respiratory","endocrine","neurological","gastrointestinal","musculoskeletal",
        "dermatological","psychiatric","immunological","infectious","oncological","renal","hematologic",
        "metabolic","congenital","autoimmune","allergic","ophthalmologic","otolaryngologic","gynecologic",
        "urologic","oral_dental","sleep","pain","toxicologic","occupational"
    ]
    cond_variants = ["acute","chronic","idiopathic","hereditary","inflammatory","degenerative","functional","structural","mild","moderate","severe","refractory"]
    for fam in cond_families:
        created += add_type(session, kg, "health_concept", f"health_condition_{fam}", f"Health condition family: {fam}")
    session.commit()
    for fam, var in product(cond_families, cond_variants):
        created += add_type(session, kg, "health_concept", f"health_condition_{fam}_{var}", f"{fam} condition, {var} variant")
    session.commit()

    symp_families = ["pain","fatigue","fever","cough","dyspnea","rash","nausea","vomiting","diarrhea","constipation","dizziness","headache","weakness","numbness","palpitations","edema","anxiety","insomnia"]
    symptom_variants = ["intermittent","persistent","progressive","sudden","localized","diffuse","exercise_induced","nocturnal","positional","postprandial"]
    for fam in symp_families:
        created += add_type(session, kg, "health_concept", f"symptom_{fam}", f"Symptom family: {fam}")
    session.commit()
    for fam, var in product(symp_families, symptom_variants):
        created += add_type(session, kg, "health_concept", f"symptom_{fam}_{var}", f"{fam} symptom, {var}")
    session.commit()

    med_classes = ["analgesic","antipyretic","antibiotic","antiviral","antifungal","antihypertensive","diuretic","antidepressant","antipsychotic","anxiolytic","steroid","bronchodilator","antihistamine","anticoagulant","antiplatelet","hypoglycemic"]
    med_variants = ["oral","topical","intravenous","subcutaneous","extended_release","short_acting","combination","pediatric","geriatric","pregnancy_safe"]
    for m in med_classes:
        created += add_type(session, kg, "health_concept", f"medication_class_{m}", f"Medication class: {m}")
    session.commit()
    for m, var in product(med_classes, med_variants):
        created += add_type(session, kg, "health_concept", f"medication_{m}_{var}", f"{m} medication, {var} formulation")
    session.commit()

    # Add mental health events
    get_or_create(session, kg, "mental_health_event", "Events related to mental health", ids["health_event"])
    session.commit()
    mental_health_events = ["therapy", "counseling"]
    for mhe in mental_health_events:
        created += add_type(session, kg, "mental_health_event", mhe, f"A session of {mhe}")
    session.commit()

    return created


def seed_creative_work_block(session, kg, ids):
    media = [
        "book","novel","short_story","poem","essay","article","research_paper","textbook",
        "movie","film","documentary","series","episode","short_film",
        "song","album","podcast","podcast_episode","audiobook","musical_score","symphony","concerto"
    ]
    genres = [
        "drama","comedy","tragedy","thriller","mystery","crime","romance","fantasy","science_fiction",
        "historical","biography","memoir","nonfiction","horror","adventure","animation","family","musical",
        "noir","western","satire","psychological","coming_of_age","war","sports","legal","medical","political",
        "educational","travel","technology","philosophy","poetry","essay","reportage","experimental","arthouse",
        "classical","jazz","blues","rock","pop","hip_hop","electronic","folk","country","ambient"
    ]
    sub_suffix = ["classic","modern","indie","mainstream","arthouse","experimental","avant_garde","neo_noir"]
    created = 0
    for m in media:
        created += add_type(session, kg, "creative_work", f"creative_{m}", f"Creative medium: {m}")
    session.commit()
    for g in genres:
        created += add_type(session, kg, "creative_work", f"creative_genre_{g}", f"Creative genre: {g}")
    session.commit()
    for g, s in product(genres, sub_suffix):
        created += add_type(session, kg, "creative_work", f"creative_subgenre_{g}_{s}", f"{g} subgenre: {s}")
    session.commit()
    return created


def seed_games_block(session, kg, ids):
    game_root = get_or_create(session, kg, "game", "Video games, board games, and other games", ids["creative_work"])
    game_types = ["video_game", "board_game", "card_game", "tabletop_rpg", "miniature_wargame", "puzzle_game"]
    for gt in game_types:
        get_or_create(session, kg, gt, f"Type of game: {gt}", game_root)
    session.commit()
    video_game_genres = ["action", "adventure", "rpg", "strategy", "simulation", "puzzle", "platformer", "shooter", "stealth", "survival", "horror", "mmo", "roguelike", "metroidvania", "visual_novel", "immersive_sim"]
    board_game_genres = ["abstract", "eurogame", "ameritrash", "wargame", "cooperative", "deck_building", "dungeon_crawl", "worker_placement", "area_control", "legacy", "social_deduction", "roll_and_write"]
    game_platforms = ["pc", "playstation", "xbox", "nintendo_switch", "mobile", "vr", "tabletop"]
    game_mechanics = ["turn_based", "real_time", "crafting", "open_world", "procedural_generation", "dice_rolling", "card_drafting", "tile_laying", "set_collection", "engine_building"]
    created = 0
    for genre in video_game_genres:
        created += add_type(session, kg, "video_game", f"video_game_genre_{genre}", f"Video game genre: {genre}")
    session.commit()
    for genre in board_game_genres:
        created += add_type(session, kg, "board_game", f"board_game_genre_{genre}", f"Board game genre: {genre}")
    session.commit()
    for platform in game_platforms:
        created += add_type(session, kg, "game", f"game_platform_{platform}", f"Gaming platform: {platform}")
    session.commit()
    for mechanic in game_mechanics:
        created += add_type(session, kg, "game", f"game_mechanic_{mechanic}", f"Game mechanic: {mechanic}")
    session.commit()
    return created


def seed_additional_concepts_block(session, kg, ids):
    created = 0
    concepts_to_add = [
        ("spirituality", "concept", "Concepts related to spirituality and belief systems"),
        ("automotive", "concept", "Concepts related to vehicles and transportation"),
        ("consumerism", "concept", "Concepts related to shopping and commercial transactions"),
        ("social_media", "concept", "Concepts related to social media platforms and culture"),
        ("relationship_building", "concept", "The abstract concept and process of building relationships"),
    ]
    for label, parent, desc in concepts_to_add:
        created += add_type(session, kg, parent, label, desc)
    session.commit()
    return created


def seed_event_blocks(session, kg, ids):
    travel_types = ["business_trip","vacation","staycation","conference_trip","family_visit","medical_travel","study_abroad","fieldwork","retreat","road_trip","camping_trip","backpacking","cruise","pilgrimage","festival_trip","tour"]
    pro_events = ["standup","sprint_planning","retrospective","demo","design_review","code_review","incident_review","brown_bag","workshop","training","interview","performance_review","quarterly_business_review","all_hands","offsite","sales_call","customer_checkin","negotiation","board_meeting","skip_level","conference_presentation", "seminar", "colloquium", "thesis_defense", "networking"]
    social_events = ["party","wedding","reunion","birthday","anniversary","dinner","lunch","coffee_chat","hike","game_night","movie_night","concert","theater","sports_match","volunteering","club_meeting","holiday_gathering", "playdate", "sleepover", "field_trip", "recital", "competition", "auction", "fundraiser", "gala", "premiere", "launch", "open_house", "tour", "screening", "tasting", "class"]
    created = 0
    for t in travel_types:
        created += add_type(session, kg, "travel", f"travel_{t}", f"Travel type: {t}")
    session.commit()
    for e in pro_events:
        created += add_type(session, kg, "professional_event", f"professional_event_{e}", f"Professional event: {e}")
    session.commit()
    for e in social_events:
        created += add_type(session, kg, "social_event", f"social_event_{e}", f"Social event: {e}")
    session.commit()
    # Environmental Events
    get_or_create(session, kg, "environmental_event", "Events related to weather and natural phenomena", get_root_id(session, "event"))
    environmental_events = ["weather_event", "natural_disaster"]
    for ee in environmental_events:
        created += add_type(session, kg, "environmental_event", ee, f"An environmental event: {ee.replace('_', ' ')}")
    session.commit()
    return created


def seed_routine_activities_block(session, kg, ids):
    """Seeds a large number of granular, daily life activities with an improved hierarchy."""
    print("   Sub-seeding routine activities...")
    created = 0
    routine_activity_id = get_or_create(session, kg, "routine_activity", "Granular daily life activities and actions", get_root_id(session, "event"))
    session.commit()

    def build_hierarchy(parent_label, data):
        nonlocal created
        parent_node = session.query(Taxonomy).filter_by(label=parent_label).first()
        for group_label, items in data.items():
            get_or_create(session, kg, group_label, f"Activities related to {group_label.replace('_', ' ')}", parent_node.id)
            session.commit()
            if isinstance(items, dict):
                build_hierarchy(group_label, items)
            elif isinstance(items, list):
                for activity_label in items:
                    created += add_type(session, kg, group_label, activity_label, f"The activity of {activity_label.replace('_', ' ')}")
                session.commit()

    activity_hierarchy = {
        "self_care": {
            "personal_care": ["shower", "bathe", "groom", "shave", "dress", "hygiene"],
            "rest_and_sleep": ["sleep", "nap", "rest", "wake", "recovery"],
        },
        "caregiving": {
            "childcare": ["feed_child", "change_diaper", "play_with_child", "read_to_child"],
            "eldercare": ["assist_elder", "administer_medication", "provide_company"],
            "pet_care": ["walk_dog", "feed_pet", "groom_pet", "train_pet", "vet_visit"],
        },
        "household": {
            "household_chore": ["laundry", "clean", "vacuum", "dust", "mop", "wash_dishes", "organize", "declutter"],
            "home_and_garden": ["renovate", "landscape", "mow", "weed", "plant", "harvest", "prune", "water", "fertilize"],
            "maintenance_and_repair": ["repair", "fix", "maintain", "install", "assemble", "disassemble", "upgrade", "troubleshoot", "diagnose", "restoration"],
            "meal": ["meal_breakfast", "meal_lunch", "meal_dinner", "meal_brunch"],
        },
        "leisure_and_hobby": {
            "consumption": ["watch", "listen", "browse_internet", "scroll", "stream"],
            "gaming": ["game", "play", "compete"],
            "spectating": ["spectate", "cheer"],
            "collecting": ["collect_stamps", "collect_coins", "collect_cards", "collect_art"],
            "craft_and_creation": ["sew", "knit", "crochet", "quilt", "embroider", "weave", "craft", "make", "build", "construct"],
            "practice_and_preparation": ["exercise_session", "practice_session", "rehearsal", "jam_session"]
        },
        "social_and_communication": {
            "communication": ["call", "text", "email", "message", "video_call", "chat"],
            "social_interaction": ["socialize", "visit", "host", "attend", "meet", "introduce", "greet", "farewell", "gather"],
            "interpersonal_dynamics": ["celebrate", "mourn", "grieve", "comfort", "support", "encourage", "praise", "thank", "apologize", "forgive", "argue", "debate", "discuss", "persuade", "compromise"],
        },
        "content_creation": {
            "digital_media": ["photograph", "film", "edit_video", "edit_photo", "post", "share"],
            "social_media_interaction": ["like", "comment", "follow", "unfollow"],
            "data_management": ["backup", "archive", "delete", "migrate", "sync", "update", "configure"],
        },
        "errands_and_transactions": {
            "errands": ["errand_grocery", "errand_banking", "errand_postal", "errand_dmv", "go_to_post_office", "pick_up_dry_cleaning", "pack", "unpack"],
            "commercial_transaction": ["shop", "browse", "purchase", "return", "sell", "donate", "recycle", "dispose"],
            "financial_transaction": ["save_money", "spend", "budget", "invest", "withdraw", "deposit", "transfer", "pay", "receive", "earn", "file_taxes"],
        },
    }
    build_hierarchy("routine_activity", activity_hierarchy)

    flat_groups = {
        "planning_and_documentation": ["schedule", "calendar", "remind", "note", "journal", "log", "track", "measure", "record", "document_life"],
        "cognitive_and_spiritual": ["pray", "worship", "contemplate", "reflect", "dream", "imagine", "brainstorm", "ideate"],
        "civic_engagement": ["civic_voting", "civic_jury_duty", "civic_town_hall", "civic_protest", "civic_volunteer", "civic_board_meeting", "civic_pta"],
        "movement_and_transport": ["commute", "drive", "ride", "fly", "queue", "wait"],
        "exploration_and_validation": ["taste", "sample", "try", "explore", "discover", "experiment", "test", "validate"],
        "health_and_recovery": ["injure", "heal", "recover", "rehabilitate", "treat", "dose"],
        "parenting_activity": ["foster", "raise", "discipline", "nurture", "bond"],
    }
    for group_label, activities in flat_groups.items():
        get_or_create(session, kg, group_label, f"Activities related to {group_label.replace('_', ' ')}", routine_activity_id)
        session.commit()
        for activity_label in activities:
            created += add_type(session, kg, group_label, activity_label, f"The activity of {activity_label.replace('_', ' ')}")
        session.commit()
    return created


def seed_state_blocks(session, kg, ids):
    relation_preds = ["friendship","mentorship","supervision","reporting_line","collegial","partnership","membership","ownership_state","tenancy","custody","guardianship","sponsorship","affiliation","alliance","rivalry","acquaintance","neighbor","classmate","teammate","roommate","spouse","parent_child","sibling","extended_family","caregiver","client_vendor","patient_provider","advisor_advisee", "internship", "apprenticeship"]
    created = 0
    for rel in relation_preds:
        created += add_type(session, kg, "relationship_state", f"relationship_{rel}", f"Relationship state: {rel}")
    session.commit()
    return created


def seed_personal_states_block(session, kg, ids):
    """
    Seed comprehensive personal state types - all temporal conditions that can change over time.
    These are NOT immutable properties - they have start_date, end_date, valid_during.
    
    Includes: preferences, interests, traits, occupational states, health states, living situations, etc.
    """
    created = 0
    print("   Seeding personal states (preferences, interests, traits, occupations, health, living)...")
    
    # ===== DIETARY & FOOD PREFERENCES (temporal states) =====
    dietary_state_id = get_or_create(session, kg, "dietary_state", "Dietary preferences and restrictions (temporal)", get_root_id(session, "state"))
    session.commit()
    
    dietary_states = [
        # Diet types
        "vegetarian", "vegan", "pescatarian", "flexitarian", "omnivore",
        "paleo", "keto", "low_carb", "mediterranean_diet", "whole_food_plant_based",
        "intermittent_fasting", "calorie_counting", "intuitive_eating",
        # Restrictions
        "gluten_free", "dairy_free", "lactose_intolerant", "nut_allergy", "shellfish_allergy",
        "soy_free", "egg_free", "kosher", "halal", "organic_only",
        # Preferences
        "health_conscious_eater", "junk_food_lover", "foodie", "picky_eater",
        "spicy_food_lover", "sweet_tooth", "savory_preference", "adventurous_eater"
    ]
    for state in dietary_states:
        created += add_type(session, kg, "dietary_state", f"dietary_{state}", f"Dietary state: {state.replace('_', ' ')}")
    session.commit()
    
    # ===== BEVERAGE PREFERENCES (temporal) =====
    beverage_state_id = get_or_create(session, kg, "beverage_preference", "Beverage consumption patterns", get_root_id(session, "state"))
    session.commit()
    
    beverage_states = [
        "coffee_drinker", "tea_drinker", "caffeine_free", "energy_drink_consumer",
        "wine_enthusiast", "beer_drinker", "cocktail_lover", "non_alcoholic",
        "water_only", "smoothie_enthusiast", "juice_drinker", "soda_drinker"
    ]
    for state in beverage_states:
        created += add_type(session, kg, "beverage_preference", f"beverage_{state}", f"Beverage preference: {state.replace('_', ' ')}")
    session.commit()
    
    # ===== INTERESTS & HOBBIES (temporal states) =====
    interest_state_id = get_or_create(session, kg, "interest_state", "Active interests and hobbies (can change)", get_root_id(session, "state"))
    session.commit()
    
    # Sports interests
    sports_interest_id = get_or_create(session, kg, "sports_interest", "Sports interests and participation", interest_state_id)
    sports_interests = [
        "runner", "cyclist", "swimmer", "hiker", "rock_climber", "yogi",
        "weightlifter", "crossfit_enthusiast", "martial_artist", "dancer",
        "football_fan", "basketball_fan", "baseball_fan", "soccer_fan", "tennis_player",
        "golfer", "skier", "snowboarder", "surfer", "skater"
    ]
    for state in sports_interests:
        created += add_type(session, kg, "sports_interest", f"sports_{state}", f"Sports interest: {state.replace('_', ' ')}")
    session.commit()
    
    # Gaming interests
    gaming_interest_id = get_or_create(session, kg, "gaming_interest", "Gaming interests", interest_state_id)
    gaming_interests = [
        "gamer", "video_gamer", "pc_gamer", "console_gamer", "mobile_gamer",
        "board_gamer", "card_gamer", "rpg_player", "strategy_gamer", "fps_player",
        "mmo_player", "casual_gamer", "competitive_gamer", "speedrunner", "streamer"
    ]
    for state in gaming_interests:
        created += add_type(session, kg, "gaming_interest", f"gaming_{state}", f"Gaming interest: {state.replace('_', ' ')}")
    session.commit()
    
    # Creative interests
    creative_interest_id = get_or_create(session, kg, "creative_interest", "Creative pursuits", interest_state_id)
    creative_interests = [
        "photographer", "amateur_photographer", "painter", "drawer", "sketcher",
        "writer", "blogger", "poet", "musician", "singer", "instrumentalist",
        "dj", "producer", "filmmaker", "video_editor", "animator",
        "graphic_designer", "digital_artist", "sculptor", "potter", "crafter",
        "knitter", "sewer", "quilter", "woodworker", "metalworker"
    ]
    for state in creative_interests:
        created += add_type(session, kg, "creative_interest", f"creative_{state}", f"Creative interest: {state.replace('_', ' ')}")
    session.commit()
    
    # Outdoor interests
    outdoor_interest_id = get_or_create(session, kg, "outdoor_interest", "Outdoor activities", interest_state_id)
    outdoor_interests = [
        "hiker", "backpacker", "camper", "angler", "fisherman", "hunter",
        "bird_watcher", "nature_photographer", "forager", "gardener",
        "rock_climber", "mountaineer", "kayaker", "sailor", "boater"
    ]
    for state in outdoor_interests:
        created += add_type(session, kg, "outdoor_interest", f"outdoor_{state}", f"Outdoor interest: {state.replace('_', ' ')}")
    session.commit()
    
    # Technology interests
    tech_interest_id = get_or_create(session, kg, "technology_interest", "Technology interests", interest_state_id)
    tech_interests = [
        "tech_enthusiast", "gadget_lover", "early_adopter", "coder", "programmer",
        "developer", "hacker", "maker", "tinkerer", "3d_printing_enthusiast",
        "robotics_enthusiast", "iot_enthusiast", "smart_home_enthusiast",
        "crypto_enthusiast", "blockchain_enthusiast", "ai_enthusiast"
    ]
    for state in tech_interests:
        created += add_type(session, kg, "technology_interest", f"tech_{state}", f"Technology interest: {state.replace('_', ' ')}")
    session.commit()
    
    # Cultural interests
    cultural_interest_id = get_or_create(session, kg, "cultural_interest", "Cultural pursuits", interest_state_id)
    cultural_interests = [
        "museum_goer", "art_lover", "theater_enthusiast", "opera_fan",
        "ballet_enthusiast", "concert_goer", "music_festival_attendee",
        "bookworm", "reader", "fiction_reader", "non_fiction_reader",
        "audiophile", "cinephile", "film_buff", "tv_series_enthusiast",
        "anime_fan", "comic_book_reader", "manga_reader"
    ]
    for state in cultural_interests:
        created += add_type(session, kg, "cultural_interest", f"cultural_{state}", f"Cultural interest: {state.replace('_', ' ')}")
    session.commit()
    
    # Collecting interests
    collecting_interest_id = get_or_create(session, kg, "collecting_interest", "Collecting hobbies", interest_state_id)
    collecting_interests = [
        "collector", "stamp_collector", "coin_collector", "card_collector",
        "action_figure_collector", "vinyl_collector", "book_collector",
        "art_collector", "antique_collector", "memorabilia_collector"
    ]
    for state in collecting_interests:
        created += add_type(session, kg, "collecting_interest", f"collecting_{state}", f"Collecting interest: {state.replace('_', ' ')}")
    session.commit()
    
    # ===== PERSONALITY TRAITS (temporal - can change) =====
    personality_state_id = get_or_create(session, kg, "personality_trait_state", "Personality traits (can evolve)", get_root_id(session, "state"))
    session.commit()
    
    personality_traits = [
        "extrovert", "introvert", "ambivert", "outgoing", "shy", "reserved",
        "optimist", "pessimist", "realist", "idealist", "pragmatist",
        "adventurous", "cautious", "spontaneous", "planner",
        "creative", "analytical", "logical", "intuitive", "empathetic",
        "assertive", "passive", "aggressive", "diplomatic", "direct",
        "patient", "impatient", "calm", "anxious", "relaxed", "stressed",
        "confident", "insecure", "humble", "proud", "modest",
        "ambitious", "laid_back", "driven", "motivated", "lazy",
        "organized", "disorganized", "neat", "messy", "minimalist", "maximalist",
        "detail_oriented", "big_picture_thinker", "perfectionist", "flexible"
    ]
    for trait in personality_traits:
        created += add_type(session, kg, "personality_trait_state", f"personality_{trait}", f"Personality trait: {trait.replace('_', ' ')}")
    session.commit()
    
    # ===== WORK STYLE (temporal) =====
    work_style_id = get_or_create(session, kg, "work_style", "Work and productivity styles", get_root_id(session, "state"))
    session.commit()
    
    work_styles = [
        "remote_worker", "office_worker", "hybrid_worker", "freelancer",
        "morning_person", "night_owl", "early_riser", "late_sleeper",
        "deadline_driven", "self_starter", "team_player", "independent_worker",
        "multitasker", "deep_work_focused", "collaborator", "solo_worker",
        "fast_paced", "methodical", "innovative", "traditional"
    ]
    for style in work_styles:
        created += add_type(session, kg, "work_style", f"work_{style}", f"Work style: {style.replace('_', ' ')}")
    session.commit()
    
    # ===== OCCUPATIONAL STATE (temporal) =====
    occupational_state_id = get_or_create(session, kg, "occupational_state", "Current occupation/role (temporal)", ids["employment"])
    session.commit()
    
    # Link to person occupation types for reuse
    occupation_states = [
        "employed", "self_employed", "unemployed", "job_seeking",
        "between_jobs", "on_sabbatical", "career_break", "parental_leave",
        "retired", "semi_retired", "consulting", "contracting", "interning"
    ]
    for state in occupation_states:
        created += add_type(session, kg, "occupational_state", f"occupation_{state}", f"Occupational state: {state.replace('_', ' ')}")
    session.commit()
    
    # ===== EDUCATIONAL STATE (temporal) =====
    educational_state_id = get_or_create(session, kg, "educational_state", "Current educational status", get_root_id(session, "state"))
    session.commit()
    
    educational_states = [
        "student", "full_time_student", "part_time_student", "online_learner",
        "graduate_student", "undergraduate_student", "phd_candidate",
        "continuing_education", "self_taught_learner", "bootcamp_attendee",
        "certificate_program", "lifelong_learner", "on_academic_break"
    ]
    for state in educational_states:
        created += add_type(session, kg, "educational_state", f"education_{state}", f"Educational state: {state.replace('_', ' ')}")
    session.commit()
    
    # ===== HEALTH & FITNESS STATE (temporal) =====
    health_fitness_state_id = get_or_create(session, kg, "health_fitness_state", "Health and fitness status", get_root_id(session, "state"))
    session.commit()
    
    health_fitness_states = [
        "active", "sedentary", "training", "recovering", "injured",
        "in_shape", "getting_in_shape", "maintaining_fitness", "building_muscle",
        "losing_weight", "gaining_weight", "marathon_training", "competition_prep",
        "physical_therapy", "rehabilitation", "healthy", "managing_condition"
    ]
    for state in health_fitness_states:
        created += add_type(session, kg, "health_fitness_state", f"fitness_{state}", f"Fitness state: {state.replace('_', ' ')}")
    session.commit()
    
    # ===== LIVING SITUATION (temporal) =====
    living_situation_id = get_or_create(session, kg, "living_situation", "Current living arrangement", get_root_id(session, "state"))
    session.commit()
    
    living_situations = [
        "homeowner", "renter", "tenant", "living_with_parents", "living_with_roommates",
        "living_alone", "cohabiting", "married_living_together", "long_distance",
        "temporary_housing", "homeless", "house_sitting", "van_life", "nomadic",
        "urban_dweller", "suburban_dweller", "rural_dweller", "city_resident", "small_town_resident"
    ]
    for situation in living_situations:
        created += add_type(session, kg, "living_situation", f"living_{situation}", f"Living situation: {situation.replace('_', ' ')}")
    session.commit()
    
    # ===== RELATIONSHIP STATUS (temporal) =====
    relationship_status_id = get_or_create(session, kg, "relationship_status", "Romantic relationship status", ids["relationship_state"])
    session.commit()
    
    relationship_statuses = [
        "single", "dating", "in_relationship", "engaged", "married", "married_separated",
        "divorced", "widowed", "its_complicated", "open_relationship", "polyamorous",
        "long_distance_relationship", "cohabiting_unmarried", "civil_union", "domestic_partnership"
    ]
    for status in relationship_statuses:
        created += add_type(session, kg, "relationship_status", f"status_{status}", f"Relationship status: {status.replace('_', ' ')}")
    session.commit()
    
    # ===== PARENTAL STATE (temporal) =====
    parental_state_id = get_or_create(session, kg, "parental_state", "Parenting status and stage", get_root_id(session, "state"))
    session.commit()
    
    parental_states = [
        "childless", "expecting_parent", "new_parent", "parent_of_infant",
        "parent_of_toddler", "parent_of_young_child", "parent_of_teen",
        "parent_of_adult_child", "empty_nester", "grandparent",
        "single_parent", "co_parent", "stay_at_home_parent", "working_parent",
        "stepparent", "adoptive_parent", "foster_parent", "guardian"
    ]
    for state in parental_states:
        created += add_type(session, kg, "parental_state", f"parenting_{state}", f"Parental state: {state.replace('_', ' ')}")
    session.commit()
    
    # ===== FINANCIAL SITUATION (temporal) =====
    financial_situation_id = get_or_create(session, kg, "financial_situation", "Current financial status", ids["financial_state"])
    session.commit()
    
    financial_situations = [
        "financially_stable", "financially_stressed", "in_debt", "debt_free",
        "building_savings", "investing", "living_paycheck_to_paycheck",
        "financially_independent", "fire_movement", "budgeting", "frugal_living",
        "high_income", "low_income", "middle_income", "wealthy", "struggling"
    ]
    for situation in financial_situations:
        created += add_type(session, kg, "financial_situation", f"financial_{situation}", f"Financial situation: {situation.replace('_', ' ')}")
    session.commit()
    
    # ===== SLEEP PATTERN (temporal) =====
    sleep_pattern_id = get_or_create(session, kg, "sleep_pattern", "Sleep habits and patterns", get_root_id(session, "state"))
    session.commit()
    
    sleep_patterns = [
        "good_sleeper", "poor_sleeper", "insomniac", "early_sleeper", "late_sleeper",
        "regular_sleep_schedule", "irregular_sleep_schedule", "shift_worker_sleep",
        "polyphasic_sleeper", "napper", "sleep_deprived", "well_rested"
    ]
    for pattern in sleep_patterns:
        created += add_type(session, kg, "sleep_pattern", f"sleep_{pattern}", f"Sleep pattern: {pattern.replace('_', ' ')}")
    session.commit()
    
    # ===== SOCIAL ENGAGEMENT (temporal) =====
    social_engagement_id = get_or_create(session, kg, "social_engagement_state", "Level of social activity", get_root_id(session, "state"))
    session.commit()
    
    social_engagement_states = [
        "socially_active", "socially_isolated", "hermit", "social_butterfly",
        "networking_actively", "making_new_friends", "maintaining_friendships",
        "introverted_period", "extroverted_period", "selective_socializer"
    ]
    for state in social_engagement_states:
        created += add_type(session, kg, "social_engagement_state", f"social_{state}", f"Social engagement: {state.replace('_', ' ')}")
    session.commit()
    
    # ===== TRAVEL STATE (temporal) =====
    travel_state_id = get_or_create(session, kg, "travel_state", "Travel patterns and status", get_root_id(session, "state"))
    session.commit()
    
    travel_states = [
        "frequent_traveler", "occasional_traveler", "non_traveler", "digital_nomad",
        "world_traveler", "local_explorer", "road_tripper", "international_traveler",
        "domestic_traveler", "business_traveler", "vacation_planner", "spontaneous_traveler"
    ]
    for state in travel_states:
        created += add_type(session, kg, "travel_state", f"travel_{state}", f"Travel state: {state.replace('_', ' ')}")
    session.commit()
    
    print(f"   ✅ Created {created} personal state types")
    return created


def seed_entity_blocks(session, kg, ids):
    sports = ["soccer","basketball","baseball","tennis","golf","swimming","track","marathon","triathlon","cycling","climbing", "esports"]
    org_types = ["company","startup","non_profit","ngo","government_agency","school","university","hospital","clinic","sports_club","research_institute","think_tank","cooperative","union","association","community_group", "law_firm"]
    loc_types = ["geographic_location","country","state_province","county","city","district","neighborhood","address","building","campus","park","region","timezone","venue","national_park", "trail"]
    poss_types = ["vehicle","real_property","bank_account","investment","equipment","instrument","computer","phone","camera","musical_instrument","book_collection","art_collection","pet_supplies", "board_game_collection"]
    pets = ["dog","cat","bird","fish","reptile","small_mammal","amphibian"]
    created = 0
    for ot in org_types:
        created += add_type(session, kg, "organization", f"organization_{ot}", f"Organization type: {ot}")
    session.commit()
    for lt in loc_types:
        created += add_type(session, kg, "location", f"location_{lt}", f"Location type: {lt}")
    session.commit()
    for pt in poss_types:
        created += add_type(session, kg, "possession", f"possession_{pt}", f"Possession type: {pt}")
    session.commit()
    for p in pets:
        created += add_type(session, kg, "pet", f"pet_{p}", f"Pet type: {p}")
    session.commit()
    for s in sports:
        created += add_type(session, kg, "entity", f"sport_{s}", f"Sport concept as entity type: {s}")
    session.commit()
    return created


def seed_family_and_social_block(session, kg, ids):
    created = 0
    family_types = ["nuclear_family", "original_family", "extended_family", "ancestors", "descendants", "lineage"]
    for ft in family_types:
        created += add_type(session, kg, "family_group", f"family_type_{ft}", f"Type of family group: {ft}")
    session.commit()
    social_types = ["close_friends_circle", "professional_network", "alumni_group", "neighborhood_community", "hobby_group", "book_club"]
    for st in social_types:
        created += add_type(session, kg, "social_group", f"social_type_{st}", f"Type of social group: {st}")
    session.commit()
    return created


def seed_person_roles_block(session, kg, ids):
    """
    Seed comprehensive person role types under entity > person.
    Includes family roles, occupations, social roles, and demographics.
    """
    created = 0
    print("   Seeding person roles (family, occupations, social roles, demographics)...")
    
    # ===== SYSTEM ROLES =====
    # Special roles for system-level entities
    created += add_type(session, kg, "person", "user", "The primary user of the system")
    created += add_type(session, kg, "person", "assistant", "AI assistant or helper")
    session.commit()
    
    # ===== FAMILY ROLES =====
    # Create family_member as umbrella category
    family_member_id = get_or_create(session, kg, "family_member", "Family relationship roles", ids["person"])
    session.commit()
    
    # Parent hierarchy
    parent_id = get_or_create(session, kg, "parent", "Parental role", family_member_id)
    for role in ["father", "mother", "stepfather", "stepmother", "adoptive_father", "adoptive_mother", "foster_father", "foster_mother"]:
        created += add_type(session, kg, "parent", role, f"Parental role: {role}")
    session.commit()
    
    # Child hierarchy
    child_id = get_or_create(session, kg, "child", "Child role", family_member_id)
    for role in ["son", "daughter", "stepson", "stepdaughter", "adopted_son", "adopted_daughter", "foster_son", "foster_daughter"]:
        created += add_type(session, kg, "child", role, f"Child role: {role}")
    session.commit()
    
    # Sibling hierarchy
    sibling_id = get_or_create(session, kg, "sibling", "Sibling role", family_member_id)
    for role in ["brother", "sister", "half_brother", "half_sister", "stepbrother", "stepsister", "twin", "twin_brother", "twin_sister"]:
        created += add_type(session, kg, "sibling", role, f"Sibling role: {role}")
    session.commit()
    
    # Grandparent hierarchy
    grandparent_id = get_or_create(session, kg, "grandparent", "Grandparent role", family_member_id)
    for role in ["grandfather", "grandmother", "paternal_grandfather", "paternal_grandmother", "maternal_grandfather", "maternal_grandmother", "great_grandfather", "great_grandmother"]:
        created += add_type(session, kg, "grandparent", role, f"Grandparent role: {role}")
    session.commit()
    
    # Grandchild hierarchy
    grandchild_id = get_or_create(session, kg, "grandchild", "Grandchild role", family_member_id)
    for role in ["grandson", "granddaughter", "great_grandson", "great_granddaughter"]:
        created += add_type(session, kg, "grandchild", role, f"Grandchild role: {role}")
    session.commit()
    
    # Spouse hierarchy
    spouse_id = get_or_create(session, kg, "spouse", "Spousal role", family_member_id)
    for role in ["husband", "wife", "partner", "fiance", "fiancee", "ex_spouse", "ex_husband", "ex_wife", "widow", "widower"]:
        created += add_type(session, kg, "spouse", role, f"Spousal/partner role: {role}")
    session.commit()
    
    # Extended family
    extended_id = get_or_create(session, kg, "extended_family_member", "Extended family roles", family_member_id)
    for role in ["aunt", "uncle", "cousin", "nephew", "niece", "great_aunt", "great_uncle", "first_cousin", "second_cousin"]:
        created += add_type(session, kg, "extended_family_member", role, f"Extended family: {role}")
    session.commit()
    
    # In-laws
    inlaw_id = get_or_create(session, kg, "in_law", "In-law relationships", family_member_id)
    for role in ["father_in_law", "mother_in_law", "son_in_law", "daughter_in_law", "brother_in_law", "sister_in_law"]:
        created += add_type(session, kg, "in_law", role, f"In-law: {role}")
    session.commit()
    
    # Godparents and other special roles
    for role in ["godparent", "godfather", "godmother", "godchild", "godson", "goddaughter", "guardian", "ward"]:
        created += add_type(session, kg, "family_member", role, f"Special family role: {role}")
    session.commit()
    
    # ===== OCCUPATIONS =====
    occupation_id = get_or_create(session, kg, "occupation", "Professional occupations and jobs", ids["person"])
    session.commit()
    
    # Technology & Engineering
    tech_id = get_or_create(session, kg, "technology_occupation", "Technology and engineering roles", occupation_id)
    tech_roles = [
        "software_engineer", "software_developer", "web_developer", "mobile_developer", "frontend_developer", 
        "backend_developer", "fullstack_developer", "devops_engineer", "site_reliability_engineer",
        "data_scientist", "data_engineer", "data_analyst", "machine_learning_engineer", "ai_researcher",
        "systems_engineer", "network_engineer", "security_engineer", "database_administrator",
        "cloud_engineer", "solutions_architect", "technical_architect", "principal_engineer",
        "engineering_manager", "chief_technology_officer", "cto"
    ]
    for role in tech_roles:
        created += add_type(session, kg, "technology_occupation", role, f"Tech role: {role.replace('_', ' ')}")
    session.commit()
    
    # Design & Creative
    creative_id = get_or_create(session, kg, "creative_occupation", "Creative and design roles", occupation_id)
    creative_roles = [
        "designer", "graphic_designer", "ux_designer", "ui_designer", "product_designer", "web_designer",
        "artist", "illustrator", "animator", "3d_artist", "concept_artist", "painter", "sculptor",
        "photographer", "videographer", "filmmaker", "video_editor", "sound_designer", "music_producer",
        "writer", "author", "journalist", "editor", "copywriter", "content_writer", "technical_writer",
        "poet", "screenwriter", "playwright", "blogger", "influencer"
    ]
    for role in creative_roles:
        created += add_type(session, kg, "creative_occupation", role, f"Creative role: {role.replace('_', ' ')}")
    session.commit()
    
    # Business & Management
    business_id = get_or_create(session, kg, "business_occupation", "Business and management roles", occupation_id)
    business_roles = [
        "manager", "project_manager", "product_manager", "program_manager", "operations_manager",
        "general_manager", "director", "vice_president", "chief_executive_officer", "ceo",
        "entrepreneur", "founder", "cofounder", "business_owner", "consultant", "business_analyst",
        "accountant", "financial_analyst", "investment_banker", "financial_advisor", "auditor",
        "marketing_manager", "sales_manager", "account_manager", "customer_success_manager",
        "human_resources_manager", "recruiter", "talent_acquisition_specialist"
    ]
    for role in business_roles:
        created += add_type(session, kg, "business_occupation", role, f"Business role: {role.replace('_', ' ')}")
    session.commit()
    
    # Healthcare
    health_id = get_or_create(session, kg, "healthcare_occupation", "Healthcare and medical roles", occupation_id)
    health_roles = [
        "doctor", "physician", "surgeon", "cardiologist", "neurologist", "psychiatrist", "pediatrician",
        "nurse", "registered_nurse", "nurse_practitioner", "physician_assistant",
        "therapist", "physical_therapist", "occupational_therapist", "speech_therapist",
        "psychologist", "counselor", "social_worker", "mental_health_counselor",
        "dentist", "orthodontist", "dental_hygienist", "pharmacist", "pharmacy_technician",
        "veterinarian", "vet_tech", "paramedic", "emt", "medical_assistant"
    ]
    for role in health_roles:
        created += add_type(session, kg, "healthcare_occupation", role, f"Healthcare role: {role.replace('_', ' ')}")
    session.commit()
    
    # Education & Research
    edu_id = get_or_create(session, kg, "education_occupation", "Education and research roles", occupation_id)
    edu_roles = [
        "teacher", "elementary_teacher", "middle_school_teacher", "high_school_teacher",
        "professor", "assistant_professor", "associate_professor", "full_professor",
        "instructor", "lecturer", "tutor", "teaching_assistant",
        "principal", "dean", "superintendent", "academic_advisor",
        "researcher", "research_scientist", "postdoctoral_researcher", "research_assistant",
        "librarian", "archivist", "curator"
    ]
    for role in edu_roles:
        created += add_type(session, kg, "education_occupation", role, f"Education role: {role.replace('_', ' ')}")
    session.commit()
    
    # Legal & Government
    legal_id = get_or_create(session, kg, "legal_occupation", "Legal and government roles", occupation_id)
    legal_roles = [
        "lawyer", "attorney", "paralegal", "legal_assistant", "judge", "magistrate",
        "prosecutor", "public_defender", "corporate_counsel", "patent_attorney",
        "politician", "senator", "representative", "mayor", "governor",
        "police_officer", "detective", "sheriff", "marshal", "security_guard",
        "firefighter", "emt", "soldier", "veteran", "military_officer"
    ]
    for role in legal_roles:
        created += add_type(session, kg, "legal_occupation", role, f"Legal/government role: {role.replace('_', ' ')}")
    session.commit()
    
    # Trades & Services
    trades_id = get_or_create(session, kg, "trades_occupation", "Trades and service roles", occupation_id)
    trades_roles = [
        "electrician", "plumber", "carpenter", "mechanic", "hvac_technician",
        "welder", "machinist", "construction_worker", "contractor", "roofer",
        "chef", "cook", "bartender", "waiter", "waitress", "server", "barista",
        "stylist", "hairdresser", "barber", "cosmetologist", "massage_therapist",
        "driver", "truck_driver", "delivery_driver", "taxi_driver", "pilot",
        "janitor", "custodian", "housekeeper", "landscaper", "gardener"
    ]
    for role in trades_roles:
        created += add_type(session, kg, "trades_occupation", role, f"Trade/service role: {role.replace('_', ' ')}")
    session.commit()
    
    # Retail & Hospitality
    retail_id = get_or_create(session, kg, "retail_occupation", "Retail and hospitality roles", occupation_id)
    retail_roles = [
        "cashier", "sales_associate", "store_manager", "retail_manager",
        "hotel_manager", "concierge", "receptionist", "front_desk_agent",
        "travel_agent", "tour_guide", "flight_attendant", "event_planner"
    ]
    for role in retail_roles:
        created += add_type(session, kg, "retail_occupation", role, f"Retail/hospitality role: {role.replace('_', ' ')}")
    session.commit()
    
    # ===== SOCIAL ROLES =====
    social_role_id = get_or_create(session, kg, "social_role", "Social and community roles", ids["person"])
    session.commit()
    
    social_roles = [
        "student", "undergraduate_student", "graduate_student", "phd_student", "alumni",
        "employee", "contractor", "intern", "apprentice", "trainee",
        "volunteer", "activist", "advocate", "organizer", "leader",
        "mentor", "mentee", "coach", "trainee", "supervisor", "supervisee",
        "colleague", "coworker", "teammate", "collaborator", "partner",
        "friend", "best_friend", "acquaintance", "neighbor", "roommate",
        "member", "board_member", "committee_member", "club_member",
        "client", "customer", "patient", "patron", "subscriber", "follower"
    ]
    for role in social_roles:
        created += add_type(session, kg, "social_role", role, f"Social role: {role.replace('_', ' ')}")
    session.commit()
    
    # ===== LIFE STAGE / DEMOGRAPHICS =====
    life_stage_id = get_or_create(session, kg, "life_stage", "Age-based life stages", ids["person"])
    session.commit()
    
    life_stages = [
        "infant", "baby", "toddler", "preschooler",
        "child", "young_child", "elementary_age_child",
        "preteen", "adolescent", "teenager", "teen", "high_schooler",
        "young_adult", "college_student", "twentysomething",
        "adult", "middle_aged_adult", "mature_adult",
        "senior", "elderly", "retiree", "centenarian"
    ]
    for stage in life_stages:
        created += add_type(session, kg, "life_stage", stage, f"Life stage: {stage.replace('_', ' ')}")
    session.commit()
    
    print(f"   ✅ Created {created} person role types")
    return created


def seed_life_milestones_block(session, kg, ids):
    created = 0
    milestones = [
        "birth", "death", "marriage", "divorce", "retirement", "first_job", "promotion", "career_change", "layoff",
        "company_founding", "high_school_graduation", "college_graduation", "masters_graduation", "phd_graduation",
        "thesis_defense", "obtaining_drivers_license", "first_home_purchase", "moving_residence", "becoming_a_parent",
        "becoming_a_grandparent", "publication_of_work", "gaining_citizenship", "adoption"
    ]
    for m in milestones:
        created += add_type(session, kg, "life_milestone", f"milestone_{m}", f"Life milestone: {m}")
    session.commit()
    return created


def seed_finance_and_home_block(session, kg, ids):
    created = 0
    finance_concepts = ["budget", "savings_account", "checking_account", "investment", "stock", "bond", "mutual_fund", "etf", "retirement_account", "401k", "ira", "pension", "debt", "loan", "mortgage", "credit_card_debt", "insurance_policy", "life_insurance", "health_insurance", "auto_insurance", "home_insurance", "tax_filing", "estate_planning", "will", "trust"]
    for fc in finance_concepts:
        created += add_type(session, kg, "financial_concept", f"finance_{fc}", f"Financial concept: {fc}")
    session.commit()
    home_concepts = ["home_ownership", "renting", "home_renovation", "home_maintenance", "gardening_project", "landscaping"]
    for hc in home_concepts:
        created += add_type(session, kg, "concept", f"home_concept_{hc}", f"Home-related concept or project: {hc}")
    session.commit()
    return created


def seed_identity_block(session, kg, ids):
    created = 0
    values = ["honesty", "creativity", "kindness", "ambition", "autonomy", "community", "curiosity", "security", "tradition"]
    for v in values:
        created += add_type(session, kg, "personal_value", f"value_{v}", f"Personal value: {v}")
    session.commit()
    identity_concepts = ["nationality", "citizenship", "spoken_language", "political_affiliation", "religious_belief", "philosophical_view"]
    for ic in identity_concepts:
        created += add_type(session, kg, "property", f"identity_{ic}", f"Aspect of personal identity: {ic}")
    session.commit()
    return created


def seed_university_life_block(session, kg, ids):
    """Seeds a comprehensive set of terms related to university and advanced education life."""
    print("   Sub-seeding university life terms...")
    created = 0

    academic_events = ["lecture", "seminar", "workshop", "workshop_series", "colloquium", "office_hours", "midterm_exam", "final_exam", "qualifying_exam", "comprehensive_exam", "oral_defense", "thesis_defense", "graduation_ceremony", "homecoming", "freshman_orientation", "registration", "internship"]
    for event in academic_events:
        created += add_type(session, kg, "educational_event", f"academic_event_{event}", f"An academic event: {event.replace('_', ' ')}")
    session.commit()

    academic_concepts = ["syllabus", "curriculum", "transcript", "gpa", "major", "minor", "credit_hour", "prerequisite", "tenure", "sabbatical", "plagiarism", "academic_probation", "dean's_list", "dissertation", "thesis", "certification_program", "bootcamp", "online_course", "mooc", "apprenticeship", "independent_study"]
    for concept in academic_concepts:
        created += add_type(session, kg, "educational_concept", f"academic_concept_{concept}", f"An academic concept: {concept.replace('_', ' ')}")
    session.commit()

    academic_roles = ["student", "undergraduate_student", "graduate_student", "phd_candidate", "postdoctoral_researcher", "professor", "assistant_professor", "associate_professor", "full_professor", "adjunct_professor", "lecturer", "teaching_assistant", "research_assistant", "dean", "provost", "chancellor", "librarian", "registrar", "academic_advisor"]
    for role in academic_roles:
        created += add_type(session, kg, "person", f"academic_role_{role}", f"An academic role: {role.replace('_', ' ')}")
    session.commit()

    academic_orgs = ["academic_department", "research_group", "laboratory", "student_club", "fraternity", "sorority", "alumni_association", "board_of_trustees", "faculty_senate"]
    for org in academic_orgs:
        created += add_type(session, kg, "organization", f"academic_org_{org}", f"An academic organization: {org.replace('_', ' ')}")
    session.commit()

    academic_locations = ["lecture_hall", "library", "laboratory", "student_union", "dormitory", "campus_quad", "registrar's_office", "admissions_office", "stadium"]
    for loc in academic_locations:
        created += add_type(session, kg, "location", f"academic_loc_{loc}", f"An academic location: {loc.replace('_', ' ')}")
    session.commit()

    academic_possessions = ["textbook", "student_id", "lab_equipment", "diploma", "class_notes"]
    for item in academic_possessions:
        created += add_type(session, kg, "possession", f"academic_item_{item}", f"An academic possession: {item.replace('_', ' ')}")
    session.commit()

    return created


def seed_mental_health_and_wellness_block(session, kg, ids):
    """Seeds a comprehensive set of terms related to mental health, therapy, and wellness."""
    print("   Sub-seeding mental health and wellness terms...")
    created = 0

    wellness_concepts = ["mindfulness", "resilience", "self_compassion", "emotional_regulation", "work_life_balance", "digital_detox", "positive_psychology", "ikigai", "flow_state"]
    for concept in wellness_concepts:
        created += add_type(session, kg, "health_concept", f"wellness_concept_{concept}", f"A wellness concept: {concept.replace('_', ' ')}")
    session.commit()

    get_or_create(session, kg, "therapy_modality", "Types and approaches of therapy", ids["health_concept"])
    therapy_modalities = ["cognitive_behavioral_therapy_cbt", "dialectical_behavior_therapy_dbt", "psychodynamic_therapy", "psychoanalysis", "humanistic_therapy", "family_therapy", "group_therapy", "art_therapy", "somatic_experiencing", "emdr", "mindfulness_based_stress_reduction_mbsr"]
    for modality in therapy_modalities:
        created += add_type(session, kg, "therapy_modality", modality, f"A therapy modality: {modality.replace('_', ' ')}")
    session.commit()

    psychiatric_conditions = ["anxiety_disorder", "depressive_disorder", "ptsd", "ocd", "bipolar_disorder", "schizophrenia", "adhd", "eating_disorder", "personality_disorder"]
    for cond in psychiatric_conditions:
        created += add_type(session, kg, "health_concept", f"health_condition_{cond}", f"A psychiatric health condition: {cond.replace('_', ' ')}")
    session.commit()

    # Create wellness_activity under routine_activity (or create it if needed)
    routine_activity_node = session.query(Taxonomy).filter_by(label="routine_activity").first()
    if not routine_activity_node:
        # If routine_activity doesn't exist yet, create under event root
        routine_activity_id = get_or_create(session, kg, "routine_activity", "Granular daily life activities and actions", get_root_id(session, "event"))
        session.commit()
    else:
        routine_activity_id = routine_activity_node.id
    
    wellness_activity_id = get_or_create(session, kg, "wellness_activity", "Activities focused on wellness and mental health", routine_activity_id)
    session.commit()
    
    wellness_activities = ["meditation", "journaling", "deep_breathing_exercise", "yoga_for_wellness", "nature_walk", "gratitude_practice"]
    for activity in wellness_activities:
        created += add_type(session, kg, "wellness_activity", activity, f"A wellness activity: {activity.replace('_', ' ')}")
    session.commit()

    therapeutic_events = ["therapy_session", "counseling_session", "support_group_meeting", "psychiatric_evaluation", "intake_session"]
    for event in therapeutic_events:
        created += add_type(session, kg, "mental_health_event", f"therapeutic_event_{event}", f"A therapeutic event: {event.replace('_', ' ')}")
    session.commit()

    mental_health_roles = ["therapist", "psychologist", "psychiatrist", "counselor", "social_worker", "life_coach"]
    for role in mental_health_roles:
        created += add_type(session, kg, "person", f"mental_health_role_{role}", f"A mental health professional role: {role.replace('_', ' ')}")
    session.commit()

    return created


def seed_scheduled_and_ceremonial_events_block(session, kg, ids):
    """Seeds specific types of appointments, ceremonies, and religious events."""
    print("   Sub-seeding scheduled and ceremonial events...")
    created = 0

    get_or_create(session, kg, "appointment", "Scheduled meetings with a professional or service", get_root_id(session, "event"))
    appointment_types = ["appointment_medical", "appointment_dental", "appointment_vision", "appointment_therapy", "appointment_legal", "appointment_financial", "appointment_haircut", "appointment_spa", "appointment_massage", "appointment_vet", "appointment_home_service", "appointment_automotive"]
    for appt in appointment_types:
        created += add_type(session, kg, "appointment", appt, f"An appointment: {appt.replace('appointment_', '').replace('_', ' ')}")
    session.commit()

    get_or_create(session, kg, "ceremony", "Formal events or rituals", get_root_id(session, "event"))
    ceremony_types = ["ceremony_graduation", "ceremony_promotion", "ceremony_award", "ceremony_inauguration", "ceremony_funeral", "ceremony_memorial", "rite_of_passage_coming_of_age", "rite_of_passage_initiation", "rite_of_passage_passage"]
    for c in ceremony_types:
        created += add_type(session, kg, "ceremony", c, f"A ceremony: {c.replace('ceremony_', '').replace('_', ' ')}")
    session.commit()

    get_or_create(session, kg, "religious_event", "Events related to religious or spiritual practice", get_root_id(session, "event"))
    religious_event_types = ["religious_service", "religious_ceremony", "religious_study", "religious_retreat"]
    for re in religious_event_types:
        created += add_type(session, kg, "religious_event", re, f"A religious event: {re.replace('religious_', '').replace('_', ' ')}")
    session.commit()

    return created


def seed_detailed_states_block(session, kg, ids):
    """Seeds a variety of detailed, long-term states."""
    print("   Sub-seeding detailed states...")
    created = 0

    def create_state_category(label, description):
        return get_or_create(session, kg, label, description, get_root_id(session, "state"))

    state_categories = {
        "membership_and_enrollment_state": ("States of belonging or access", ["enrollment", "membership", "subscription"]),
        "residence_state": ("States related to living arrangements", ["residence", "tenancy"]),
        "health_and_wellness_state": ("States of physical and mental health", ["pregnancy", "illness_episode", "recovery", "remission", "treatment", "therapy", "sobriety", "abstinence", "diet_adherence", "training_regimen"]),
        "legal_and_contractual_state": ("States defined by legal or formal agreements", ["probation", "parole", "suspension", "litigation", "contract", "lease", "warranty", "custody", "guardianship"]),
        "program_participation_state": ("States of being in a specific program", ["training_program", "fellowship"]),
        "status_and_progression_state": ("States of being in a queue or process", ["visa_status", "residency_status", "citizenship_application", "waiting_list", "application_pending", "approval_pending"]),
        "platform_access_state": ("States related to access on digital platforms", ["ban", "suspension_from_platform", "restriction"]),
    }

    for cat_label, (cat_desc, state_list) in state_categories.items():
        create_state_category(cat_label, cat_desc)
        session.commit()
        for state_label in state_list:
            created += add_type(session, kg, cat_label, state_label, f"The state of {state_label.replace('_', ' ')}")
        session.commit()

    created += add_type(session, kg, "employment", "unemployment", "The state of being unemployed")
    created += add_type(session, kg, "employment", "disability", "The state of being on disability")
    created += add_type(session, kg, "employment", "retirement", "The state of being retired")
    created += add_type(session, kg, "employment", "leave_of_absence", "The state of being on a leave of absence")
    created += add_type(session, kg, "employment", "sabbatical", "The state of being on sabbatical")

    created += add_type(session, kg, "relationship_state", "engagement", "The state of being engaged to be married")
    created += add_type(session, kg, "relationship_state", "separation", "The state of being separated from a partner")
    created += add_type(session, kg, "relationship_state", "widowhood", "The state of being a widow or widower")
    created += add_type(session, kg, "relationship_state", "caregiving", "The state of being a primary caregiver")

    created += add_type(session, kg, "financial_state", "bankruptcy", "The state of being in bankruptcy")
    created += add_type(session, kg, "financial_state", "debt", "The state of being in debt")
    created += add_type(session, kg, "financial_state", "solvency", "The state of being solvent")

    return created


def seed_detailed_entities_block(session, kg, ids):
    """Seeds a large number of specific entity types across various domains."""
    print("   Sub-seeding detailed entities...")
    created = 0

    def build_hierarchy(parent_label, data):
        nonlocal created
        parent_node = session.query(Taxonomy).filter_by(label=parent_label).first()
        for group_label, items in data.items():
            get_or_create(session, kg, group_label, f"Category for {group_label.replace('_', ' ')}", parent_node.id)
            session.commit()
            if isinstance(items, dict):
                build_hierarchy(group_label, items)
            elif isinstance(items, list):
                for entity_label in items:
                    created += add_type(session, kg, group_label, entity_label, f"An entity of type {entity_label.replace('_', ' ')}")
                session.commit()

    entity_hierarchy = {
        "entity": {
            "commercial_entity": ["brand", "product", "service"],
            "digital_entity": ["app", "website", "platform", "api", "library", "framework", "protocol", "tool"],
            "fictional_or_abstract_person": ["character", "fictional_character", "mythological_figure", "deity"],
            "biological_entity": ["species", "breed", "cultivar", "disease", "pathogen", "virus", "bacteria"],
            "astronomical_entity": ["celestial_body", "constellation"],
        },
        "person": {
            "creative_role": ["artist", "author", "director", "musician", "actor", "creator"],
            "public_figure": ["influencer", "historical_figure", "celebrity"],
        },
        "social_group": {
            "performance_group": ["band", "ensemble", "cast", "crew"],
            "sports_team": ["team"],
        },
        "organization": {
            "governing_body": ["committee", "board", "council", "congress", "parliament"],
        },
        "possession": {
            "financial_instrument": ["currency", "cryptocurrency", "stock", "commodity", "index"],
            "vehicle": ["vehicle_car", "vehicle_motorcycle", "vehicle_bicycle", "vehicle_boat"],
            "home_good": ["furniture", "appliance", "electronics"],
            "personal_item": ["jewelry", "clothing"],
            "art_and_collectibles": ["artwork", "sculpture", "photograph", "memorabilia", "heirloom"],
            "document": ["document", "certificate", "license", "passport", "deed"],
            "digital_asset": ["recording", "footage", "archive", "backup", "snapshot"],
        },
        "location": {
            "geographic_feature": ["mountain", "river", "ocean", "island", "continent", "ecosystem", "habitat"],
            "man_made_structure": ["landmark", "monument", "memorial", "highway", "bridge", "tunnel", "dam"],
            "venue_and_building": ["museum", "library_building", "stadium", "arena", "theater_venue", "concert_hall", "restaurant", "store", "mall", "market", "airport", "station"],
        }
    }

    build_hierarchy("entity", entity_hierarchy["entity"])
    build_hierarchy("person", entity_hierarchy["person"])
    build_hierarchy("social_group", entity_hierarchy["social_group"])
    build_hierarchy("organization", entity_hierarchy["organization"])
    build_hierarchy("possession", entity_hierarchy["possession"])
    build_hierarchy("location", entity_hierarchy["location"])

    return created


def seed_goals_block(session, kg, ids):
    """Seeds a comprehensive hierarchy of goal types."""
    print("   Sub-seeding goals...")
    created = 0

    def build_hierarchy(parent_label, data):
        nonlocal created
        parent_node = session.query(Taxonomy).filter_by(label=parent_label).first()
        for group_label, items in data.items():
            get_or_create(session, kg, group_label, f"Category for {group_label.replace('_', ' ')} goals", parent_node.id)
            session.commit()
            if isinstance(items, list):
                for goal_label in items:
                    created += add_type(session, kg, group_label, goal_label, f"A goal related to {goal_label.replace('_', ' ')}")
                session.commit()

    goal_hierarchy = {
        "health_goal": ["health", "fitness", "weight", "nutrition", "sleep", "mental_health"],
        "financial_goal": ["financial", "savings", "investment", "retirement", "debt_reduction", "income"],
        "career_goal": ["career", "promotion", "job_change", "skill_development", "certification"],
        "education_goal": ["education", "degree", "course_completion", "language_learning", "reading"],
        "creative_goal": ["creative", "publication", "performance", "exhibition"],
        "relationship_goal": ["relationship", "friendship", "romantic", "family", "networking"],
        "personal_growth_goal": ["personal_growth", "habit_formation", "habit_breaking", "self_improvement"],
        "experiential_goal": ["spiritual", "travel", "adventure", "experience", "collection"],
        "home_goal": ["home", "renovation", "organization", "minimalism"],
        "social_impact_goal": ["social", "volunteering", "community", "activism"],
        "goal_timeframe": ["short_term", "medium_term", "long_term", "lifetime", "daily", "weekly", "monthly", "quarterly", "annual"],
    }
    build_hierarchy("goal", goal_hierarchy)
    return created


def seed_rituals_block(session, kg, ids):
    """Seeds a hierarchy of recurring, meaningful practices and traditions (rituals) as states."""
    print("   Sub-seeding rituals (as states)...")
    created = 0

    ritual_id = get_or_create(session, kg, "ritual", "The state of practicing a recurring, meaningful tradition", get_root_id(session, "state"))
    session.commit()

    def build_hierarchy(parent_label, data):
        nonlocal created
        parent_node = session.query(Taxonomy).filter_by(label=parent_label).first()
        for group_label, items in data.items():
            get_or_create(session, kg, group_label, f"Category for states of practicing {group_label.replace('_', ' ')} rituals", parent_node.id)
            session.commit()
            if isinstance(items, list):
                for ritual_label in items:
                    created += add_type(session, kg, group_label, ritual_label, f"The state of practicing the ritual of {ritual_label.replace('_', ' ')}")
                session.commit()

    ritual_hierarchy = {
        "daily_ritual": ["morning_coffee", "evening_walk", "bedtime_routine", "meditation_session", "journaling", "gratitude_practice"],
        "weekly_ritual": ["family_dinner", "phone_call_parent", "sabbath", "game_night", "meal_prep", "review_session", "planning_session"],
        "monthly_ritual": ["financial_review", "reflection", "goal_review"],
        "annual_ritual": ["birthday", "anniversary", "holiday_tradition", "vacation", "review_annual", "goal_setting"],
        "social_ritual": ["greeting", "farewell", "toast", "celebration"],
        "spiritual_ritual": ["prayer", "worship", "ceremony", "pilgrimage"],
        "professional_ritual": ["standup", "retrospective", "one_on_one"],
        "creative_ritual": ["practice", "warm_up", "creative_session"],
        "relationship_ritual": ["date_night", "check_in", "shared_activity"]
    }
    build_hierarchy("ritual", ritual_hierarchy)
    return created


def seed_projects_block(session, kg, ids):
    """Seeds a hierarchy for projects as entities and goals."""
    print("   Sub-seeding projects (as entities and goals)...")
    created = 0

    # Project as an Entity
    project_id = get_or_create(session, kg, "project", "A planned piece of work that has a specific purpose", get_root_id(session, "entity"))
    session.commit()
    project_types = [
        "home_improvement_project", "software_development_project", "research_project",
        "creative_project", "business_project", "community_project", "personal_project"
    ]
    for p_type in project_types:
        created += add_type(session, kg, "project", p_type, f"A type of project: {p_type.replace('_', ' ')}")
    session.commit()

    # Project as a Goal
    project_goal_id = get_or_create(session, kg, "project_goal", "Goals related to the lifecycle of a project", get_root_id(session, "goal"))
    session.commit()
    project_goals = ["project_completion", "project_milestone_achieved", "project_proposal_approval"]
    for p_goal in project_goals:
        created += add_type(session, kg, "project_goal", p_goal, f"A project goal: {p_goal.replace('_', ' ')}")
    session.commit()

    # Project states
    created += add_type(session, kg, "state", "project_state", "States of a project's lifecycle")
    session.commit()
    project_states = ["project_active", "project_paused", "project_completed"]
    for p_state in project_states:
        created += add_type(session, kg, "project_state", p_state, f"A project state: {p_state.replace('_', ' ')}")
    session.commit()

    return created


if __name__ == "__main__":
    main()

