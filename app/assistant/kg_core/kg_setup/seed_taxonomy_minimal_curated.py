"""
Seed Minimal Curated Taxonomy - Strictly Following IS-A Relationships

CORE PRINCIPLE: Every child must satisfy "child IS-A parent"
- ✅ scientist IS-A professional IS-A person
- ❌ "Apple Inc." is NOT A type, it's an instance
- ❌ "California" is NOT A type, it's an instance

This creates ~200-300 essential types for 80% of common usage.
Types are CLASSIFICATIONS, not instances.
"""

from app.models.base import get_session
from app.assistant.kg_core.taxonomy.models import Taxonomy
from app.assistant.kg_core.kg_utils.knowledge_graph_utils import KnowledgeGraphUtils


def main():
    print("🌱 Seeding minimal curated taxonomy with strict IS-A relationships...")
    session = get_session()
    kg = KnowledgeGraphUtils(session)
    
    try:
        # Check if already seeded
        sentinel = session.query(Taxonomy).filter_by(label="minimal_curated_taxonomy_seeded").first()
        if sentinel:
            print("✅ Minimal curated taxonomy already seeded.")
            return
        
        # Create root types
        roots = ensure_roots(session, kg)
        
        # Build taxonomy hierarchically
        print("\n📦 Building ENTITY branch...")
        build_entity_branch(session, kg, roots['entity'])
        
        print("\n📦 Building EVENT branch...")
        build_event_branch(session, kg, roots['event'])
        
        print("\n📦 Building STATE branch...")
        build_state_branch(session, kg, roots['state'])
        
        print("\n📦 Building GOAL branch...")
        build_goal_branch(session, kg, roots['goal'])
        
        print("\n📦 Building CONCEPT branch...")
        build_concept_branch(session, kg, roots['concept'])
        
        print("\n📦 Building PROPERTY branch...")
        build_property_branch(session, kg, roots['property'])
        
        # Add sentinel
        add_type(session, kg, roots['concept'], "minimal_curated_taxonomy_seeded", 
                "Marker for minimal curated taxonomy")
        
        session.commit()
        
        total = session.query(Taxonomy).count()
        print(f"\n✅ Seeded {total} taxonomy types")
        
    except Exception as e:
        print(f"\n❌ Error: {e}")
        session.rollback()
        raise
    finally:
        session.close()


def ensure_roots(session, kg):
    """Create the 6 root taxonomy types."""
    roots = {}
    
    root_types = [
        ("entity", "Physical or abstract things that exist"),
        ("event", "Actions or occurrences at a point in time"),
        ("state", "Conditions or situations that persist over time"),
        ("goal", "Desired outcomes or objectives"),
        ("concept", "Abstract ideas or categories of knowledge"),
        ("property", "Attributes or characteristics")
    ]
    
    for label, desc in root_types:
        tax = session.query(Taxonomy).filter_by(label=label, parent_id=None).first()
        if not tax:
            tax = Taxonomy(
                label=label,
                description=desc,
                parent_id=None,
                label_embedding=kg.create_embedding(label)
            )
            session.add(tax)
            session.flush()
        roots[label] = tax.id
    
    session.commit()
    return roots


def build_entity_branch(session, kg, entity_id):
    """
    Build ENTITY branch - things that exist.
    
    IS-A Rule: Every child IS A type of entity
    """
    
    # ============ PERSON ============
    # person IS-A entity ✅
    person_id = add_type(session, kg, entity_id, "person", 
                        "A human being")
    
    # user IS-A person ✅ (system users like Alex)
    add_type(session, kg, person_id, "user", 
            "Primary user of the system")
    
    # Family roles (types of familial relationships)
    # parent IS-A person ✅
    add_type(session, kg, person_id, "parent", "A person who has children")
    add_type(session, kg, person_id, "child", "A person in relation to their parents")
    add_type(session, kg, person_id, "sibling", "A person who shares parents with another")
    add_type(session, kg, person_id, "spouse", "A married person in relation to their partner")
    
    # Social roles
    # friend IS-A person ✅
    add_type(session, kg, person_id, "friend", "A person with whom one has a bond of mutual affection")
    add_type(session, kg, person_id, "acquaintance", "A person one knows slightly")
    add_type(session, kg, person_id, "colleague", "A person with whom one works")
    add_type(session, kg, person_id, "neighbor", "A person living nearby")
    
    # Professional types
    # professional IS-A person ✅
    professional_id = add_type(session, kg, person_id, "professional", 
                              "A person engaged in a specified profession")
    
    # Specific professions (each IS-A professional)
    add_type(session, kg, professional_id, "scientist", "A person engaged in scientific research")
    add_type(session, kg, professional_id, "engineer", "A person who designs, builds, or maintains systems")
    add_type(session, kg, professional_id, "doctor", "A medical professional")
    add_type(session, kg, professional_id, "teacher", "An educator")
    add_type(session, kg, professional_id, "lawyer", "A legal professional")
    add_type(session, kg, professional_id, "artist", "A person who creates art")
    add_type(session, kg, professional_id, "writer", "A person who writes professionally")
    add_type(session, kg, professional_id, "developer", "A software developer")
    add_type(session, kg, professional_id, "researcher", "A person who conducts research")
    
    # Public figures
    # public_figure IS-A person ✅
    public_figure_id = add_type(session, kg, person_id, "public_figure", 
                                "A person of public interest or fame")
    add_type(session, kg, public_figure_id, "celebrity", "A famous person")
    add_type(session, kg, public_figure_id, "politician", "A person involved in politics")
    add_type(session, kg, public_figure_id, "athlete", "A person proficient in sports")
    
    # ============ ORGANIZATION ============
    # organization IS-A entity ✅
    org_id = add_type(session, kg, entity_id, "organization", 
                     "A structured group of people with a common purpose")
    
    # company IS-A organization ✅
    company_id = add_type(session, kg, org_id, "company", 
                         "A commercial business organization")
    add_type(session, kg, company_id, "tech_company", "A technology company")
    add_type(session, kg, company_id, "startup", "An early-stage company")
    add_type(session, kg, company_id, "corporation", "A large company")
    
    # Educational institutions
    # school IS-A organization ✅
    school_id = add_type(session, kg, org_id, "school", 
                        "An educational institution")
    add_type(session, kg, school_id, "university", "A higher education institution")
    add_type(session, kg, school_id, "college", "An educational institution")
    add_type(session, kg, school_id, "high_school", "A secondary school")
    
    # Other org types
    add_type(session, kg, org_id, "government", "A governing body")
    add_type(session, kg, org_id, "ngo", "A non-governmental organization")
    add_type(session, kg, org_id, "nonprofit", "A non-profit organization")
    add_type(session, kg, org_id, "club", "A membership organization")
    add_type(session, kg, org_id, "team", "A group organized for a purpose")
    
    # ============ LOCATION ============
    # location IS-A entity ✅
    loc_id = add_type(session, kg, entity_id, "location", 
                     "A place or position")
    
    # Geographic types (NOT instances!)
    # country IS-A location ✅ (the TYPE country, not "USA")
    add_type(session, kg, loc_id, "country", "A nation state")
    add_type(session, kg, loc_id, "state", "A state or province")
    add_type(session, kg, loc_id, "city", "An urban area")
    add_type(session, kg, loc_id, "town", "A small urban area")
    add_type(session, kg, loc_id, "neighborhood", "A district within a city")
    
    # Physical places (types, not instances)
    # building IS-A location ✅
    building_id = add_type(session, kg, loc_id, "building", 
                          "A structure with walls and a roof")
    add_type(session, kg, building_id, "home", "A dwelling place")
    add_type(session, kg, building_id, "office", "A workplace")
    add_type(session, kg, building_id, "school_building", "An educational facility")
    add_type(session, kg, building_id, "hospital", "A medical facility")
    add_type(session, kg, building_id, "restaurant", "A dining establishment")
    add_type(session, kg, building_id, "store", "A retail establishment")
    add_type(session, kg, building_id, "gym", "A fitness facility")
    
    # Outdoor locations
    # outdoor_location IS-A location ✅
    outdoor_id = add_type(session, kg, loc_id, "outdoor_location", 
                         "A location in the open air")
    add_type(session, kg, outdoor_id, "park", "A public green space")
    add_type(session, kg, outdoor_id, "beach", "A shore area")
    add_type(session, kg, outdoor_id, "mountain", "An elevated landform")
    add_type(session, kg, outdoor_id, "forest", "A wooded area")
    add_type(session, kg, outdoor_id, "trail", "A path for walking or hiking")
    
    # ============ AI AGENT ============
    # ai_agent IS-A entity ✅
    ai_id = add_type(session, kg, entity_id, "ai_agent", 
                    "An artificial intelligence agent")
    add_type(session, kg, ai_id, "assistant", "An AI assistant")
    add_type(session, kg, ai_id, "chatbot", "A conversational AI")
    add_type(session, kg, ai_id, "agent", "An autonomous AI system")
    
    # ============ ANIMAL ============
    # animal IS-A entity ✅
    animal_id = add_type(session, kg, entity_id, "animal", 
                        "A living organism")
    
    # pet IS-A animal ✅
    pet_id = add_type(session, kg, animal_id, "pet", 
                     "A domesticated animal kept for companionship")
    add_type(session, kg, pet_id, "dog", "A domesticated canine")
    add_type(session, kg, pet_id, "cat", "A domesticated feline")
    add_type(session, kg, pet_id, "bird", "An avian pet")
    add_type(session, kg, pet_id, "fish", "An aquatic pet")
    
    # ============ ARTIFACT ============
    # artifact IS-A entity ✅
    artifact_id = add_type(session, kg, entity_id, "artifact", 
                          "A human-made object or creation")
    
    # ============ CREATIVE WORK ============
    # creative_work IS-A artifact ✅ (moved from entity)
    creative_id = add_type(session, kg, artifact_id, "creative_work", 
                          "A work of creative expression")
    
    # Media types
    add_type(session, kg, creative_id, "book", "A written or printed work")
    add_type(session, kg, creative_id, "article", "A written piece")
    add_type(session, kg, creative_id, "movie", "A motion picture")
    add_type(session, kg, creative_id, "tv_show", "A television program")
    add_type(session, kg, creative_id, "video", "A recorded visual medium")
    add_type(session, kg, creative_id, "music", "Musical composition or recording")
    add_type(session, kg, creative_id, "song", "A musical piece")
    add_type(session, kg, creative_id, "podcast", "An audio program")
    add_type(session, kg, creative_id, "game", "An interactive entertainment")
    add_type(session, kg, creative_id, "artwork", "A piece of visual art")
    add_type(session, kg, creative_id, "photograph", "A captured image")
    
    # ============ DIGITAL ENTITY ============
    # digital_entity IS-A artifact ✅ (moved from entity)
    digital_id = add_type(session, kg, artifact_id, "digital_entity", 
                         "A digital or software entity")
    
    add_type(session, kg, digital_id, "software", "A computer program")
    add_type(session, kg, digital_id, "application", "A software application")
    add_type(session, kg, digital_id, "website", "A web presence")
    add_type(session, kg, digital_id, "platform", "A software platform")
    add_type(session, kg, digital_id, "tool", "A software tool")
    add_type(session, kg, digital_id, "service", "An online service")
    add_type(session, kg, digital_id, "database", "A data storage system")
    add_type(session, kg, digital_id, "api", "An application programming interface")
    
    # digital_document IS-A digital_entity ✅
    doc_id = add_type(session, kg, digital_id, "digital_document", 
                     "A digital file or document")
    add_type(session, kg, doc_id, "document", "A text document")
    add_type(session, kg, doc_id, "spreadsheet", "A tabular document")
    add_type(session, kg, doc_id, "presentation", "A slide deck")
    add_type(session, kg, doc_id, "code_file", "A source code file")
    
    # ============ POSSESSION ============
    # possession IS-A entity ✅
    possession_id = add_type(session, kg, entity_id, "possession", 
                            "A physical object owned")
    
    # vehicle IS-A possession ✅
    vehicle_id = add_type(session, kg, possession_id, "vehicle", 
                         "A means of transportation")
    add_type(session, kg, vehicle_id, "car", "An automobile")
    add_type(session, kg, vehicle_id, "bicycle", "A two-wheeled vehicle")
    add_type(session, kg, vehicle_id, "motorcycle", "A motorized two-wheeler")
    
    # device IS-A possession ✅
    device_id = add_type(session, kg, possession_id, "device", 
                        "An electronic device")
    add_type(session, kg, device_id, "phone", "A mobile phone")
    add_type(session, kg, device_id, "computer", "A computing device")
    add_type(session, kg, device_id, "tablet", "A tablet device")
    add_type(session, kg, device_id, "wearable", "A wearable device")
    
    # ============ PRODUCT ============
    # product IS-A artifact ✅ (changed from entity)
    product_id = add_type(session, kg, artifact_id, "product", 
                         "A commercial product or offering")
    
    # physical_product IS-A product ✅
    physical_product_id = add_type(session, kg, product_id, "physical_product", 
                                   "A tangible product")
    add_type(session, kg, physical_product_id, "consumer_good", "A consumer product")
    add_type(session, kg, physical_product_id, "furniture", "Furniture product")
    add_type(session, kg, physical_product_id, "clothing", "Clothing item")
    add_type(session, kg, physical_product_id, "appliance", "Household appliance")
    
    # service IS-A product ✅
    service_id = add_type(session, kg, product_id, "service", 
                          "A service offering")
    add_type(session, kg, service_id, "professional_service", "Professional service")
    add_type(session, kg, service_id, "subscription", "Subscription service")
    add_type(session, kg, service_id, "utility", "Utility service")
    
    # ============ FOOD ============
    # food IS-A entity ✅
    food_id = add_type(session, kg, entity_id, "food", 
                      "Edible substance")
    
    add_type(session, kg, food_id, "meal", "A meal")
    add_type(session, kg, food_id, "ingredient", "A food ingredient")
    add_type(session, kg, food_id, "beverage", "A drink")
    add_type(session, kg, food_id, "dish", "A prepared dish")
    add_type(session, kg, food_id, "snack", "A snack food")
    
    # ============ MEDICAL ENTITY ============
    # medical_entity IS-A entity ✅
    medical_id = add_type(session, kg, entity_id, "medical_entity", 
                         "A medical-related entity")
    
    add_type(session, kg, medical_id, "medication", "A pharmaceutical drug")
    add_type(session, kg, medical_id, "treatment", "A medical treatment")
    add_type(session, kg, medical_id, "medical_condition", "A health condition")
    add_type(session, kg, medical_id, "symptom", "A medical symptom")
    add_type(session, kg, medical_id, "medical_procedure", "A medical procedure")
    
    # ============ INTANGIBLE ============
    # intangible IS-A entity ✅
    intangible_id = add_type(session, kg, entity_id, "intangible", 
                            "An intangible entity")
    
    add_type(session, kg, intangible_id, "intellectual_property", "Intellectual property")
    add_type(session, kg, intangible_id, "license", "A license or permission")
    add_type(session, kg, intangible_id, "right", "A right or entitlement")
    add_type(session, kg, intangible_id, "warranty", "A warranty or guarantee")
    
    # ============ PLANT ============
    # plant IS-A entity ✅
    plant_id = add_type(session, kg, entity_id, "plant", 
                       "A plant organism")
    
    add_type(session, kg, plant_id, "tree", "A tree plant")
    add_type(session, kg, plant_id, "flower", "A flowering plant")
    add_type(session, kg, plant_id, "vegetable_plant", "A vegetable plant")
    add_type(session, kg, plant_id, "herb", "An herb plant")
    
    session.commit()


def build_event_branch(session, kg, event_id):
    """
    Build EVENT branch - actions or occurrences.
    
    IS-A Rule: Every child IS A type of event
    """
    
    # ============ COMMUNICATION ============
    # communication IS-A event ✅
    comm_id = add_type(session, kg, event_id, "communication", 
                      "An act of information exchange")
    
    add_type(session, kg, comm_id, "conversation", "An informal exchange of ideas")
    add_type(session, kg, comm_id, "discussion", "A detailed consideration of a topic")
    add_type(session, kg, comm_id, "meeting", "A formal gathering for discussion")
    add_type(session, kg, comm_id, "presentation", "A formal talk")
    add_type(session, kg, comm_id, "call", "A phone conversation")
    add_type(session, kg, comm_id, "video_call", "A video conversation")
    add_type(session, kg, comm_id, "interview", "A formal questioning session")
    add_type(session, kg, comm_id, "negotiation", "A discussion to reach agreement")
    
    # Text-based communication
    add_type(session, kg, comm_id, "message", "A brief communication")
    add_type(session, kg, comm_id, "email", "An electronic message")
    add_type(session, kg, comm_id, "text", "A text message")
    add_type(session, kg, comm_id, "chat", "An online conversation")
    
    # ============ SOCIAL EVENT ============
    # social_event IS-A event ✅
    social_id = add_type(session, kg, event_id, "social_event", 
                        "A social gathering")
    
    add_type(session, kg, social_id, "party", "A social celebration")
    add_type(session, kg, social_id, "dinner", "A social meal")
    add_type(session, kg, social_id, "lunch", "A midday meal gathering")
    add_type(session, kg, social_id, "celebration", "A joyous social event")
    add_type(session, kg, social_id, "gathering", "An informal meeting of people")
    add_type(session, kg, social_id, "hangout", "A casual social meeting")
    
    # Specific celebrations
    # ceremony IS-A social_event ✅
    ceremony_id = add_type(session, kg, social_id, "ceremony", 
                          "A formal social event")
    add_type(session, kg, ceremony_id, "wedding", "A marriage ceremony")
    add_type(session, kg, ceremony_id, "birthday_party", "A birthday celebration")
    add_type(session, kg, ceremony_id, "anniversary", "An anniversary celebration")
    add_type(session, kg, ceremony_id, "graduation", "A graduation ceremony")
    
    # ============ PROFESSIONAL EVENT ============
    # professional_event IS-A event ✅
    prof_id = add_type(session, kg, event_id, "professional_event", 
                      "A work-related event")
    
    add_type(session, kg, prof_id, "conference", "A large professional gathering")
    add_type(session, kg, prof_id, "workshop", "A hands-on training session")
    add_type(session, kg, prof_id, "seminar", "An educational session")
    add_type(session, kg, prof_id, "training", "A skill development session")
    add_type(session, kg, prof_id, "team_meeting", "A team coordination meeting")
    add_type(session, kg, prof_id, "project_review", "A project status review")
    add_type(session, kg, prof_id, "performance_review", "An employee evaluation")
    
    # ============ PHYSICAL ACTIVITY ============
    # physical_activity IS-A event ✅
    phys_id = add_type(session, kg, event_id, "physical_activity", 
                      "A physical action or exercise")
    
    # exercise IS-A physical_activity ✅
    exercise_id = add_type(session, kg, phys_id, "exercise", 
                          "A physical exertion for fitness")
    add_type(session, kg, exercise_id, "workout", "A structured exercise session")
    add_type(session, kg, exercise_id, "cardio", "Cardiovascular exercise")
    add_type(session, kg, exercise_id, "strength_training", "Resistance exercise")
    add_type(session, kg, exercise_id, "stretching", "Flexibility exercise")
    
    # sport IS-A physical_activity ✅
    sport_id = add_type(session, kg, phys_id, "sport", 
                       "A competitive physical activity")
    add_type(session, kg, sport_id, "running", "A running sport")
    add_type(session, kg, sport_id, "swimming", "A swimming sport")
    add_type(session, kg, sport_id, "cycling", "A cycling sport")
    add_type(session, kg, sport_id, "basketball", "A basketball sport")
    add_type(session, kg, sport_id, "soccer", "A soccer sport")
    add_type(session, kg, sport_id, "tennis", "A tennis sport")
    add_type(session, kg, sport_id, "hiking", "A hiking activity")
    
    # Other physical activities
    add_type(session, kg, phys_id, "yoga", "A mind-body practice")
    add_type(session, kg, phys_id, "meditation", "A mindfulness practice")
    add_type(session, kg, phys_id, "walk", "A walking activity")
    add_type(session, kg, phys_id, "dance", "A dancing activity")
    
    # ============ EDUCATIONAL EVENT ============
    # educational_event IS-A event ✅
    edu_id = add_type(session, kg, event_id, "educational_event", 
                     "A learning event")
    
    add_type(session, kg, edu_id, "class", "An instructional session")
    add_type(session, kg, edu_id, "lecture", "A formal educational talk")
    add_type(session, kg, edu_id, "tutorial", "A hands-on learning session")
    add_type(session, kg, edu_id, "study_session", "A self-study period")
    add_type(session, kg, edu_id, "exam", "An assessment")
    add_type(session, kg, edu_id, "quiz", "A short assessment")
    add_type(session, kg, edu_id, "lab", "A laboratory session")
    
    # ============ APPOINTMENT ============
    # appointment IS-A event ✅
    appt_id = add_type(session, kg, event_id, "appointment", 
                      "A scheduled meeting")
    
    add_type(session, kg, appt_id, "medical_appointment", "A healthcare appointment")
    add_type(session, kg, appt_id, "dental_appointment", "A dental appointment")
    add_type(session, kg, appt_id, "service_appointment", "A service appointment")
    
    # ============ TRAVEL ============
    # travel IS-A event ✅
    travel_id = add_type(session, kg, event_id, "travel", 
                        "Movement from one place to another")
    
    add_type(session, kg, travel_id, "trip", "A journey")
    add_type(session, kg, travel_id, "vacation", "A leisure trip")
    add_type(session, kg, travel_id, "business_trip", "A work-related trip")
    add_type(session, kg, travel_id, "commute", "Regular travel to work")
    add_type(session, kg, travel_id, "flight", "Air travel")
    
    # ============ ROUTINE ACTIVITY ============
    # routine_activity IS-A event ✅
    routine_id = add_type(session, kg, event_id, "routine_activity", 
                         "A regular daily activity")
    
    add_type(session, kg, routine_id, "sleep", "Resting activity")
    add_type(session, kg, routine_id, "meal", "An eating occasion")
    add_type(session, kg, routine_id, "breakfast", "Morning meal")
    add_type(session, kg, routine_id, "cooking", "Food preparation")
    add_type(session, kg, routine_id, "cleaning", "Housekeeping")
    add_type(session, kg, routine_id, "shopping", "Purchasing goods")
    add_type(session, kg, routine_id, "errands", "Small tasks")
    
    # ============ TRANSACTION ============
    # transaction IS-A event ✅
    transaction_id = add_type(session, kg, event_id, "transaction", 
                             "A financial or commercial transaction")
    
    add_type(session, kg, transaction_id, "purchase", "A buying transaction")
    add_type(session, kg, transaction_id, "sale", "A selling transaction")
    add_type(session, kg, transaction_id, "payment", "A payment transaction")
    add_type(session, kg, transaction_id, "refund", "A refund transaction")
    add_type(session, kg, transaction_id, "donation", "A donation transaction")
    add_type(session, kg, transaction_id, "rental", "A rental transaction")
    
    # ============ LEGAL EVENT ============
    # legal_event IS-A event ✅
    legal_id = add_type(session, kg, event_id, "legal_event", 
                       "A legal proceeding or action")
    
    add_type(session, kg, legal_id, "contract_signing", "Signing a contract")
    add_type(session, kg, legal_id, "lawsuit", "A legal lawsuit")
    add_type(session, kg, legal_id, "court_hearing", "A court proceeding")
    add_type(session, kg, legal_id, "arbitration", "An arbitration proceeding")
    
    # ============ ACHIEVEMENT ============
    # achievement IS-A event ✅
    achievement_id = add_type(session, kg, event_id, "achievement", 
                             "An accomplishment or success")
    
    add_type(session, kg, achievement_id, "award", "Receiving an award")
    add_type(session, kg, achievement_id, "recognition", "Receiving recognition")
    add_type(session, kg, achievement_id, "completion", "Completing something")
    add_type(session, kg, achievement_id, "milestone", "Reaching a milestone")
    
    # ============ LIFE MILESTONE ============
    # life_milestone IS-A event ✅
    milestone_id = add_type(session, kg, event_id, "life_milestone", 
                           "A significant life event")
    
    add_type(session, kg, milestone_id, "birth", "A birth")
    add_type(session, kg, milestone_id, "death", "A death")
    add_type(session, kg, milestone_id, "coming_of_age", "Coming of age")
    add_type(session, kg, milestone_id, "retirement", "Retirement")
    
    # ============ ACCIDENT ============
    # accident IS-A event ✅
    accident_id = add_type(session, kg, event_id, "accident", 
                          "An unintended incident")
    
    add_type(session, kg, accident_id, "injury", "An injury accident")
    add_type(session, kg, accident_id, "damage", "Property damage")
    add_type(session, kg, accident_id, "spill", "A spill accident")
    
    # ============ CREATIVE EVENT ============
    # creative_event IS-A event ✅
    creative_event_id = add_type(session, kg, event_id, "creative_event", 
                                "A creative activity")
    
    add_type(session, kg, creative_event_id, "writing", "Writing activity")
    add_type(session, kg, creative_event_id, "drawing", "Drawing activity")
    add_type(session, kg, creative_event_id, "composing", "Music composition")
    add_type(session, kg, creative_event_id, "designing", "Design activity")
    
    session.commit()


def build_state_branch(session, kg, state_id):
    """
    Build STATE branch - conditions that persist over time.
    
    IS-A Rule: Every child IS A type of state
    """
    
    # ============ RELATIONSHIP ============
    # relationship IS-A state ✅
    rel_id = add_type(session, kg, state_id, "relationship", 
                     "A connection between entities")
    
    add_type(session, kg, rel_id, "friendship", "A friend relationship")
    add_type(session, kg, rel_id, "marriage", "A married relationship")
    add_type(session, kg, rel_id, "partnership", "A partner relationship")
    add_type(session, kg, rel_id, "dating", "A romantic relationship")
    add_type(session, kg, rel_id, "engagement", "An engaged relationship")
    add_type(session, kg, rel_id, "family_relationship", "A family connection")
    add_type(session, kg, rel_id, "professional_relationship", "A work relationship")
    add_type(session, kg, rel_id, "mentorship", "A mentor-mentee relationship")
    add_type(session, kg, rel_id, "ownership", "An ownership relationship")
    add_type(session, kg, rel_id, "membership", "A membership relationship")
    
    # ============ EMPLOYMENT ============
    # employment_status IS-A state ✅
    emp_id = add_type(session, kg, state_id, "employment_status", 
                     "A work status")
    
    add_type(session, kg, emp_id, "employed", "Having a job")
    add_type(session, kg, emp_id, "unemployed", "Not having a job")
    add_type(session, kg, emp_id, "self_employed", "Working for oneself")
    add_type(session, kg, emp_id, "freelance", "Independent contractor")
    add_type(session, kg, emp_id, "retired", "No longer working")
    add_type(session, kg, emp_id, "student", "In education")
    add_type(session, kg, emp_id, "intern", "In an internship")
    
    # ============ EMOTIONAL STATE ============
    # emotional_state IS-A state ✅
    emo_id = add_type(session, kg, state_id, "emotional_state", 
                     "An emotional condition")
    
    # positive_emotion IS-A emotional_state ✅
    pos_id = add_type(session, kg, emo_id, "positive_emotion", 
                     "A positive feeling")
    add_type(session, kg, pos_id, "happiness", "A state of joy")
    add_type(session, kg, pos_id, "excitement", "A state of enthusiasm")
    add_type(session, kg, pos_id, "contentment", "A state of satisfaction")
    add_type(session, kg, pos_id, "gratitude", "A state of thankfulness")
    add_type(session, kg, pos_id, "love", "A state of affection")
    
    # negative_emotion IS-A emotional_state ✅
    neg_id = add_type(session, kg, emo_id, "negative_emotion", 
                     "A negative feeling")
    add_type(session, kg, neg_id, "sadness", "A state of sorrow")
    add_type(session, kg, neg_id, "anger", "A state of displeasure")
    add_type(session, kg, neg_id, "anxiety", "A state of worry")
    add_type(session, kg, neg_id, "fear", "A state of apprehension")
    add_type(session, kg, neg_id, "frustration", "A state of dissatisfaction")
    add_type(session, kg, neg_id, "stress", "A state of tension")
    
    # neutral_emotion IS-A emotional_state ✅
    neutral_id = add_type(session, kg, emo_id, "neutral_emotion", 
                         "A neutral feeling")
    add_type(session, kg, neutral_id, "calm", "A state of tranquility")
    add_type(session, kg, neutral_id, "boredom", "A state of disinterest")
    add_type(session, kg, neutral_id, "confusion", "A state of uncertainty")
    
    # ============ HEALTH STATUS ============
    # health_status IS-A state ✅
    health_id = add_type(session, kg, state_id, "health_status", 
                        "A health condition")
    
    add_type(session, kg, health_id, "healthy", "In good health")
    add_type(session, kg, health_id, "sick", "Having an illness")
    add_type(session, kg, health_id, "recovering", "Regaining health")
    add_type(session, kg, health_id, "injured", "Having an injury")
    add_type(session, kg, health_id, "fatigued", "Experiencing tiredness")
    add_type(session, kg, health_id, "energetic", "Having high energy")
    add_type(session, kg, health_id, "pain", "Experiencing discomfort")
    
    # ============ LIVING SITUATION ============
    # living_situation IS-A state ✅
    living_id = add_type(session, kg, state_id, "living_situation", 
                        "A residential status")
    
    add_type(session, kg, living_id, "renting", "Renting a residence")
    add_type(session, kg, living_id, "owning", "Owning a residence")
    add_type(session, kg, living_id, "living_alone", "Residing alone")
    add_type(session, kg, living_id, "living_with_roommates", "Residing with roommates")
    add_type(session, kg, living_id, "living_with_family", "Residing with family")
    add_type(session, kg, living_id, "living_with_partner", "Residing with partner")
    
    # ============ SKILL LEVEL ============
    # skill_level IS-A state ✅
    skill_id = add_type(session, kg, state_id, "skill_level", 
                       "A proficiency level")
    
    add_type(session, kg, skill_id, "beginner", "Novice level")
    add_type(session, kg, skill_id, "intermediate", "Moderate skill level")
    add_type(session, kg, skill_id, "advanced", "High skill level")
    add_type(session, kg, skill_id, "expert", "Mastery level")
    
    # ============ FINANCIAL STATUS ============
    # financial_status IS-A state ✅
    fin_id = add_type(session, kg, state_id, "financial_status", 
                     "A financial condition")
    
    add_type(session, kg, fin_id, "debt", "Having financial obligations")
    add_type(session, kg, fin_id, "savings", "Having money saved")
    add_type(session, kg, fin_id, "investing", "Actively investing")
    add_type(session, kg, fin_id, "budgeting", "Managing finances")
    
    # ============ INTEREST ============
    # interest IS-A state ✅ (ongoing affinity for a topic)
    interest_id = add_type(session, kg, state_id, "interest", 
                          "A sustained curiosity or affinity")
    
    add_type(session, kg, interest_id, "hobby", "A leisure interest")
    add_type(session, kg, interest_id, "passion", "A strong interest")
    add_type(session, kg, interest_id, "curiosity", "An exploratory interest")
    
    # ============ LEGAL STATUS ============
    # legal_status IS-A state ✅
    legal_status_id = add_type(session, kg, state_id, "legal_status", 
                              "A legal standing or status")
    
    add_type(session, kg, legal_status_id, "citizenship", "Citizenship status")
    add_type(session, kg, legal_status_id, "legal_capacity", "Legal capacity status")
    add_type(session, kg, legal_status_id, "visa_status", "Immigration status")
    add_type(session, kg, legal_status_id, "liability", "Legal liability")
    
    # ============ AVAILABILITY ============
    # availability IS-A state ✅
    availability_id = add_type(session, kg, state_id, "availability", 
                              "An availability status")
    
    add_type(session, kg, availability_id, "available", "Available status")
    add_type(session, kg, availability_id, "unavailable", "Unavailable status")
    add_type(session, kg, availability_id, "reserved", "Reserved status")
    add_type(session, kg, availability_id, "sold_out", "Sold out status")
    add_type(session, kg, availability_id, "in_stock", "In stock status")
    
    # ============ CONDITION ============
    # condition IS-A state ✅
    condition_id = add_type(session, kg, state_id, "condition", 
                           "A physical condition")
    
    add_type(session, kg, condition_id, "new", "New condition")
    add_type(session, kg, condition_id, "used", "Used condition")
    add_type(session, kg, condition_id, "broken", "Broken condition")
    add_type(session, kg, condition_id, "damaged", "Damaged condition")
    add_type(session, kg, condition_id, "refurbished", "Refurbished condition")
    
    # ============ MEMBERSHIP STATUS ============
    # membership_status IS-A state ✅
    membership_id = add_type(session, kg, state_id, "membership_status", 
                            "A membership state")
    
    add_type(session, kg, membership_id, "active_member", "Active membership")
    add_type(session, kg, membership_id, "expired_member", "Expired membership")
    add_type(session, kg, membership_id, "suspended_member", "Suspended membership")
    add_type(session, kg, membership_id, "pending_member", "Pending membership")
    
    # ============ EDUCATIONAL STATUS ============
    # educational_status IS-A state ✅
    edu_status_id = add_type(session, kg, state_id, "educational_status", 
                            "An education-related status")
    
    add_type(session, kg, edu_status_id, "enrolled", "Enrolled in education")
    add_type(session, kg, edu_status_id, "graduated", "Graduated status")
    add_type(session, kg, edu_status_id, "dropped_out", "Dropped out status")
    add_type(session, kg, edu_status_id, "on_leave", "On leave status")
    
    session.commit()


def build_goal_branch(session, kg, goal_id):
    """
    Build GOAL branch - desired outcomes.
    
    IS-A Rule: Every child IS A type of goal
    """
    
    # ============ PERSONAL GOAL ============
    # personal_goal IS-A goal ✅
    personal_id = add_type(session, kg, goal_id, "personal_goal", 
                          "A personal objective")
    
    # health_goal IS-A personal_goal ✅
    health_id = add_type(session, kg, personal_id, "health_goal", 
                        "A health-related objective")
    add_type(session, kg, health_id, "weight_loss", "Goal to lose weight")
    add_type(session, kg, health_id, "fitness", "Goal to improve fitness")
    add_type(session, kg, health_id, "nutrition", "Goal to improve diet")
    add_type(session, kg, health_id, "quit_smoking", "Goal to stop smoking")
    add_type(session, kg, health_id, "sleep_improvement", "Goal to improve sleep")
    
    # learning_goal IS-A personal_goal ✅
    learning_id = add_type(session, kg, personal_id, "learning_goal", 
                          "A learning objective")
    add_type(session, kg, learning_id, "learn_language", "Goal to learn a language")
    add_type(session, kg, learning_id, "learn_skill", "Goal to learn a skill")
    add_type(session, kg, learning_id, "read_books", "Goal to read more")
    add_type(session, kg, learning_id, "take_course", "Goal to complete a course")
    
    # habit_goal IS-A personal_goal ✅
    habit_id = add_type(session, kg, personal_id, "habit_goal", 
                       "A habit-forming objective")
    add_type(session, kg, habit_id, "meditation_practice", "Goal to meditate regularly")
    add_type(session, kg, habit_id, "exercise_routine", "Goal to exercise regularly")
    add_type(session, kg, habit_id, "reading_habit", "Goal to read regularly")
    add_type(session, kg, habit_id, "writing_habit", "Goal to write regularly")
    
    # ============ PROFESSIONAL GOAL ============
    # professional_goal IS-A goal ✅
    prof_id = add_type(session, kg, goal_id, "professional_goal", 
                      "A career objective")
    
    add_type(session, kg, prof_id, "career_advancement", "Goal for promotion")
    add_type(session, kg, prof_id, "job_change", "Goal to switch jobs")
    add_type(session, kg, prof_id, "skill_development", "Goal to develop skills")
    add_type(session, kg, prof_id, "certification", "Goal to get certified")
    add_type(session, kg, prof_id, "leadership", "Goal to become a leader")
    add_type(session, kg, prof_id, "entrepreneurship", "Goal to start a business")
    
    # ============ FINANCIAL GOAL ============
    # financial_goal IS-A goal ✅
    fin_id = add_type(session, kg, goal_id, "financial_goal", 
                     "A financial objective")
    
    add_type(session, kg, fin_id, "save_money", "Goal to save funds")
    add_type(session, kg, fin_id, "pay_off_debt", "Goal to eliminate debt")
    add_type(session, kg, fin_id, "invest", "Goal to invest")
    add_type(session, kg, fin_id, "buy_home", "Goal to purchase property")
    add_type(session, kg, fin_id, "retirement_planning", "Goal to plan retirement")
    
    # ============ PROJECT GOAL ============
    # project_goal IS-A goal ✅
    proj_id = add_type(session, kg, goal_id, "project_goal", 
                      "A project objective")
    
    add_type(session, kg, proj_id, "complete_project", "Goal to finish a project")
    add_type(session, kg, proj_id, "launch_product", "Goal to launch something")
    add_type(session, kg, proj_id, "write_book", "Goal to write a book")
    add_type(session, kg, proj_id, "build_app", "Goal to build an application")
    
    # ============ RELATIONSHIP GOAL ============
    # relationship_goal IS-A goal ✅
    rel_id = add_type(session, kg, goal_id, "relationship_goal", 
                     "A relationship objective")
    
    add_type(session, kg, rel_id, "make_friends", "Goal to form friendships")
    add_type(session, kg, rel_id, "improve_relationship", "Goal to strengthen bonds")
    add_type(session, kg, rel_id, "find_partner", "Goal to find a romantic partner")
    add_type(session, kg, rel_id, "start_family", "Goal to have children")
    
    session.commit()


def build_concept_branch(session, kg, concept_id):
    """
    Build CONCEPT branch - abstract ideas.
    
    IS-A Rule: Every child IS A type of concept
    """
    
    # ============ TECHNOLOGY ============
    # technology IS-A concept ✅
    tech_id = add_type(session, kg, concept_id, "technology", 
                      "A technological concept")
    
    # ai IS-A technology ✅
    ai_id = add_type(session, kg, tech_id, "ai", 
                    "Artificial intelligence")
    add_type(session, kg, ai_id, "machine_learning", "ML concept")
    add_type(session, kg, ai_id, "deep_learning", "Deep learning concept")
    add_type(session, kg, ai_id, "neural_network", "Neural network concept")
    add_type(session, kg, ai_id, "natural_language_processing", "NLP concept")
    add_type(session, kg, ai_id, "computer_vision", "Computer vision concept")
    
    # programming IS-A technology ✅
    prog_id = add_type(session, kg, tech_id, "programming", 
                      "Programming concept")
    add_type(session, kg, prog_id, "programming_language", "A programming language concept")
    add_type(session, kg, prog_id, "web_development", "Web dev concept")
    add_type(session, kg, prog_id, "mobile_development", "Mobile dev concept")
    add_type(session, kg, prog_id, "data_science", "Data science concept")
    add_type(session, kg, prog_id, "software_engineering", "Software eng concept")
    add_type(session, kg, prog_id, "devops", "DevOps concept")
    add_type(session, kg, prog_id, "cloud_computing", "Cloud concept")
    add_type(session, kg, prog_id, "database", "Database concept")
    
    # ============ ACADEMIC SUBJECT ============
    # academic_subject IS-A concept ✅
    academic_id = add_type(session, kg, concept_id, "academic_subject", 
                          "An area of study")
    
    # science IS-A academic_subject ✅
    science_id = add_type(session, kg, academic_id, "science", 
                         "Scientific field")
    add_type(session, kg, science_id, "physics", "Physics concept")
    add_type(session, kg, science_id, "chemistry", "Chemistry concept")
    add_type(session, kg, science_id, "biology", "Biology concept")
    add_type(session, kg, science_id, "astronomy", "Astronomy concept")
    add_type(session, kg, science_id, "geology", "Geology concept")
    
    # mathematics IS-A academic_subject ✅
    math_id = add_type(session, kg, academic_id, "mathematics", 
                      "Mathematical field")
    add_type(session, kg, math_id, "algebra", "Algebra concept")
    add_type(session, kg, math_id, "calculus", "Calculus concept")
    add_type(session, kg, math_id, "statistics", "Statistics concept")
    add_type(session, kg, math_id, "geometry", "Geometry concept")
    
    # humanities IS-A academic_subject ✅
    hum_id = add_type(session, kg, academic_id, "humanities", 
                     "Humanities field")
    add_type(session, kg, hum_id, "history", "History concept")
    add_type(session, kg, hum_id, "philosophy", "Philosophy concept")
    add_type(session, kg, hum_id, "literature", "Literature concept")
    add_type(session, kg, hum_id, "language", "Language concept")
    add_type(session, kg, hum_id, "art", "Art concept")
    
    # social_science IS-A academic_subject ✅
    social_id = add_type(session, kg, academic_id, "social_science", 
                        "Social science field")
    add_type(session, kg, social_id, "psychology", "Psychology concept")
    add_type(session, kg, social_id, "sociology", "Sociology concept")
    add_type(session, kg, social_id, "economics", "Economics concept")
    add_type(session, kg, social_id, "political_science", "Political science concept")
    
    # ============ BUSINESS ============
    # business IS-A concept ✅
    business_id = add_type(session, kg, concept_id, "business", 
                          "Business concept")
    
    add_type(session, kg, business_id, "marketing", "Marketing concept")
    add_type(session, kg, business_id, "finance", "Finance concept")
    add_type(session, kg, business_id, "management", "Management concept")
    add_type(session, kg, business_id, "entrepreneurship", "Entrepreneurship concept")
    add_type(session, kg, business_id, "strategy", "Strategy concept")
    add_type(session, kg, business_id, "operations", "Operations concept")
    add_type(session, kg, business_id, "human_resources", "HR concept")
    
    # ============ WELLNESS ============
    # wellness IS-A concept ✅
    wellness_id = add_type(session, kg, concept_id, "wellness", 
                          "Wellness concept")
    
    add_type(session, kg, wellness_id, "nutrition", "Nutrition concept")
    add_type(session, kg, wellness_id, "mindfulness", "Mindfulness concept")
    add_type(session, kg, wellness_id, "fitness", "Fitness concept")
    add_type(session, kg, wellness_id, "sleep", "Sleep concept")
    add_type(session, kg, wellness_id, "stress_management", "Stress management concept")
    add_type(session, kg, wellness_id, "mental_health", "Mental health concept")
    
    # ============ INDUSTRY ============
    # industry IS-A concept ✅
    industry_id = add_type(session, kg, concept_id, "industry", 
                          "An industry sector")
    
    add_type(session, kg, industry_id, "agriculture", "Agriculture industry")
    add_type(session, kg, industry_id, "manufacturing", "Manufacturing industry")
    add_type(session, kg, industry_id, "retail", "Retail industry")
    add_type(session, kg, industry_id, "hospitality", "Hospitality industry")
    add_type(session, kg, industry_id, "healthcare", "Healthcare industry")
    add_type(session, kg, industry_id, "education_industry", "Education industry")
    add_type(session, kg, industry_id, "transportation", "Transportation industry")
    add_type(session, kg, industry_id, "energy", "Energy industry")
    
    # ============ METHODOLOGY ============
    # methodology IS-A concept ✅
    methodology_id = add_type(session, kg, concept_id, "methodology", 
                             "A systematic approach or method")
    
    add_type(session, kg, methodology_id, "agile", "Agile methodology")
    add_type(session, kg, methodology_id, "waterfall", "Waterfall methodology")
    add_type(session, kg, methodology_id, "scientific_method", "Scientific method")
    add_type(session, kg, methodology_id, "design_thinking", "Design thinking")
    add_type(session, kg, methodology_id, "lean", "Lean methodology")
    
    # ============ GENERAL TOPICS ============
    add_type(session, kg, concept_id, "politics", "Political concepts")
    add_type(session, kg, concept_id, "sports", "Sports concepts")
    add_type(session, kg, concept_id, "entertainment", "Entertainment concepts")
    add_type(session, kg, concept_id, "culture", "Cultural concepts")
    add_type(session, kg, concept_id, "religion", "Religious concepts")
    add_type(session, kg, concept_id, "environment", "Environmental concepts")
    add_type(session, kg, concept_id, "law", "Legal concepts")
    add_type(session, kg, concept_id, "ethics", "Ethical concepts")
    add_type(session, kg, concept_id, "philosophy", "Philosophical concepts")
    
    session.commit()


def build_property_branch(session, kg, property_id):
    """
    Build PROPERTY branch - attributes/characteristics.
    
    IS-A Rule: Every child IS A type of property
    """
    
    # ============ DEMOGRAPHIC ============
    # demographic IS-A property ✅
    demo_id = add_type(session, kg, property_id, "demographic", 
                      "A demographic attribute")
    
    add_type(session, kg, demo_id, "age", "Age property")
    add_type(session, kg, demo_id, "gender", "Gender property")
    add_type(session, kg, demo_id, "nationality", "Nationality property")
    add_type(session, kg, demo_id, "ethnicity", "Ethnicity property")
    add_type(session, kg, demo_id, "language_spoken", "Language property")
    
    # ============ PHYSICAL ============
    # physical_property IS-A property ✅
    phys_id = add_type(session, kg, property_id, "physical_property", 
                      "A physical attribute")
    
    add_type(session, kg, phys_id, "height", "Height property")
    add_type(session, kg, phys_id, "weight", "Weight property")
    add_type(session, kg, phys_id, "color", "Color property")
    add_type(session, kg, phys_id, "size", "Size property")
    add_type(session, kg, phys_id, "shape", "Shape property")
    add_type(session, kg, phys_id, "temperature", "Temperature property")
    
    # ============ QUANTITATIVE ============
    # quantitative_property IS-A property ✅
    quant_id = add_type(session, kg, property_id, "quantitative_property", 
                       "A measurable quantity")
    
    add_type(session, kg, quant_id, "quantity", "Amount property")
    add_type(session, kg, quant_id, "duration", "Time duration property")
    add_type(session, kg, quant_id, "distance", "Distance property")
    add_type(session, kg, quant_id, "speed", "Speed property")
    add_type(session, kg, quant_id, "cost", "Cost property")
    
    # ============ QUALITATIVE ============
    # qualitative_property IS-A property ✅
    qual_id = add_type(session, kg, property_id, "qualitative_property", 
                      "A qualitative attribute")
    
    add_type(session, kg, qual_id, "quality", "Quality property")
    add_type(session, kg, qual_id, "difficulty", "Difficulty level")
    add_type(session, kg, qual_id, "priority", "Priority level")
    add_type(session, kg, qual_id, "urgency", "Urgency level")
    add_type(session, kg, qual_id, "importance", "Importance level")
    
    session.commit()


def add_type(session, kg, parent_label_or_id, label, description, prints=True):
    """
    Add a taxonomy type under a parent.
    
    Args:
        parent_label_or_id: Parent's label string or ID integer
        label: Label for new type (lowercase_snake_case)
        description: Human-readable description
        prints: Whether to print (throttled for large batches)
    
    Returns:
        The new taxonomy ID
    """
    # Find parent
    if isinstance(parent_label_or_id, int):
        parent_id = parent_label_or_id
    else:
        parent = session.query(Taxonomy).filter_by(label=parent_label_or_id).first()
        if not parent:
            raise ValueError(f"Parent '{parent_label_or_id}' not found")
        parent_id = parent.id
    
    # Check if already exists
    existing = session.query(Taxonomy).filter_by(
        label=label,
        parent_id=parent_id
    ).first()
    
    if existing:
        return existing.id
    
    # Create new type
    tax = Taxonomy(
        label=label,
        description=description,
        parent_id=parent_id,
        label_embedding=kg.create_embedding(label)
    )
    session.add(tax)
    session.flush()
    
    if prints:
        parent_obj = session.query(Taxonomy).filter_by(id=parent_id).first()
        parent_label = parent_obj.label if parent_obj else "ROOT"
        print(f"   + {parent_label} > {label}")
    
    return tax.id


if __name__ == "__main__":
    main()

