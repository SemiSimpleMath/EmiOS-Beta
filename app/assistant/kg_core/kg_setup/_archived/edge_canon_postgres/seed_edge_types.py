"""
Seed comprehensive edge types for the knowledge graph.
Edges describe participation roles in States, Events, and Goals.

Pattern: Entity --[role]--> State/Event/Goal

Run this script to populate the edge_canon table with canonical edge types.
"""

from app.assistant.kg.db.knowledge_graph_db import get_session

# Comprehensive edge type registry organized by pattern
EDGE_TYPES = {
    # ==========================================
    # OWNERSHIP & POSSESSION STATES
    # ==========================================
    "ownership_state": [
        {
            "edge_type": "owner_in",
            "inverse": "owned_in",
            "domain": "Entity",
            "range": "State",
            "description": "Entity is the owner in an ownership state",
            "example": "Alex --[owner_in]--> OwnershipState",
            "aliases": ["owns_in", "possessor_in", "has_ownership_in"]
        },
        {
            "edge_type": "owned_in",
            "inverse": "owner_in",
            "domain": "Entity",
            "range": "State",
            "description": "Entity is the owned object in an ownership state",
            "example": "Scout --[owned_in]--> OwnershipState",
            "aliases": ["possessed_in", "owned_by_in", "property_in"]
        },
        {
            "edge_type": "co_owner_in",
            "is_symmetric": True,
            "domain": "Entity",
            "range": "State",
            "description": "Entity is a co-owner (joint ownership)",
            "example": "Spouse1 --[co_owner_in]--> OwnershipState",
            "aliases": ["joint_owner_in", "shared_owner_in"]
        },
    ],
    
    # ==========================================
    # EMPLOYMENT & PROFESSIONAL STATES
    # ==========================================
    "employment_state": [
        {
            "edge_type": "employee_in",
            "inverse": "employer_in",
            "domain": "Entity",
            "range": "State",
            "description": "Entity is the employee in an employment relationship",
            "example": "Alex --[employee_in]--> EmploymentState",
            "aliases": ["worker_in", "staff_member_in", "employed_in"]
        },
        {
            "edge_type": "employer_in",
            "inverse": "employee_in",
            "domain": "Entity",
            "range": "State",
            "description": "Entity is the employer in an employment relationship",
            "example": "Microsoft --[employer_in]--> EmploymentState",
            "aliases": ["company_in", "organization_in", "hires_in"]
        },
        {
            "edge_type": "manager_in",
            "domain": "Entity",
            "range": "State",
            "description": "Entity is the manager in a management relationship",
            "example": "Boss --[manager_in]--> ManagementState",
            "aliases": ["supervisor_in", "lead_in", "boss_in"]
        },
        {
            "edge_type": "managed_in",
            "inverse": "manager_in",
            "domain": "Entity",
            "range": "State",
            "description": "Entity is managed in a management relationship",
            "example": "Alex --[managed_in]--> ManagementState",
            "aliases": ["reports_to_in", "supervised_in", "direct_report_in"]
        },
        {
            "edge_type": "colleague_in",
            "is_symmetric": True,
            "domain": "Entity",
            "range": "State",
            "description": "Entity is a colleague in a work relationship",
            "example": "Alex --[colleague_in]--> ColleagueState",
            "aliases": ["coworker_in", "teammate_in", "peer_in"]
        },
        {
            "edge_type": "contractor_in",
            "domain": "Entity",
            "range": "State",
            "description": "Entity is a contractor (not employee)",
            "example": "Freelancer --[contractor_in]--> ContractState",
            "aliases": ["freelancer_in", "consultant_in", "vendor_in"]
        },
        {
            "edge_type": "client_in",
            "inverse": "contractor_in",
            "domain": "Entity",
            "range": "State",
            "description": "Entity is the client hiring a contractor",
            "example": "Company --[client_in]--> ContractState",
            "aliases": ["customer_in", "buyer_in"]
        },
    ],
    
    # ==========================================
    # FAMILY RELATIONSHIP STATES
    # ==========================================
    "family_relationship_state": [
        {
            "edge_type": "parent_in",
            "inverse": "child_in",
            "domain": "Entity",
            "range": "State",
            "description": "Entity is the parent in a parent-child relationship",
            "example": "Father --[parent_in]--> ParentingState",
            "aliases": ["father_in", "mother_in", "parenthood_in"]
        },
        {
            "edge_type": "child_in",
            "inverse": "parent_in",
            "domain": "Entity",
            "range": "State",
            "description": "Entity is the child in a parent-child relationship",
            "example": "Son --[child_in]--> ParentingState",
            "aliases": ["son_in", "daughter_in", "offspring_in"]
        },
        {
            "edge_type": "spouse_in",
            "is_symmetric": True,
            "domain": "Entity",
            "range": "State",
            "description": "Entity is a spouse in a marriage",
            "example": "Husband --[spouse_in]--> MarriageState",
            "aliases": ["married_to_in", "husband_in", "wife_in", "partner_in"]
        },
        {
            "edge_type": "sibling_in",
            "is_symmetric": True,
            "domain": "Entity",
            "range": "State",
            "description": "Entity is a sibling in a sibling relationship",
            "example": "Brother --[sibling_in]--> SiblingState",
            "aliases": ["brother_in", "sister_in"]
        },
        {
            "edge_type": "grandparent_in",
            "inverse": "grandchild_in",
            "domain": "Entity",
            "range": "State",
            "description": "Entity is the grandparent",
            "example": "Grandma --[grandparent_in]--> GrandparentingState",
            "aliases": ["grandfather_in", "grandmother_in"]
        },
        {
            "edge_type": "grandchild_in",
            "inverse": "grandparent_in",
            "domain": "Entity",
            "range": "State",
            "description": "Entity is the grandchild",
            "example": "Grandson --[grandchild_in]--> GrandparentingState",
            "aliases": ["grandson_in", "granddaughter_in"]
        },
        {
            "edge_type": "aunt_uncle_in",
            "inverse": "niece_nephew_in",
            "domain": "Entity",
            "range": "State",
            "description": "Entity is an aunt or uncle",
            "example": "Uncle --[aunt_uncle_in]--> ExtendedFamilyState",
            "aliases": ["aunt_in", "uncle_in"]
        },
        {
            "edge_type": "niece_nephew_in",
            "inverse": "aunt_uncle_in",
            "domain": "Entity",
            "range": "State",
            "description": "Entity is a niece or nephew",
            "example": "Niece --[niece_nephew_in]--> ExtendedFamilyState",
            "aliases": ["niece_in", "nephew_in"]
        },
        {
            "edge_type": "cousin_in",
            "is_symmetric": True,
            "domain": "Entity",
            "range": "State",
            "description": "Entity is a cousin",
            "example": "Cousin --[cousin_in]--> CousinState",
            "aliases": []
        },
        {
            "edge_type": "stepparent_in",
            "inverse": "stepchild_in",
            "domain": "Entity",
            "range": "State",
            "description": "Entity is a stepparent",
            "example": "Stepfather --[stepparent_in]--> StepfamilyState",
            "aliases": ["stepfather_in", "stepmother_in"]
        },
        {
            "edge_type": "stepchild_in",
            "inverse": "stepparent_in",
            "domain": "Entity",
            "range": "State",
            "description": "Entity is a stepchild",
            "example": "Stepson --[stepchild_in]--> StepfamilyState",
            "aliases": ["stepson_in", "stepdaughter_in"]
        },
        {
            "edge_type": "in_law_in",
            "is_symmetric": True,
            "domain": "Entity",
            "range": "State",
            "description": "Entity is an in-law relation",
            "example": "Mother-in-law --[in_law_in]--> InLawState",
            "aliases": ["mother_in_law_in", "father_in_law_in", "brother_in_law_in"]
        },
    ],
    
    # ==========================================
    # SOCIAL RELATIONSHIP STATES
    # ==========================================
    "social_relationship_state": [
        {
            "edge_type": "friend_in",
            "is_symmetric": True,
            "domain": "Entity",
            "range": "State",
            "description": "Entity is a friend in a friendship",
            "example": "Alice --[friend_in]--> FriendshipState",
            "aliases": ["friendship_with_in", "buddy_in", "pal_in"]
        },
        {
            "edge_type": "best_friend_in",
            "is_symmetric": True,
            "domain": "Entity",
            "range": "State",
            "description": "Entity is a best friend (close friendship)",
            "example": "BFF --[best_friend_in]--> BestFriendshipState",
            "aliases": ["bff_in", "close_friend_in"]
        },
        {
            "edge_type": "acquaintance_in",
            "is_symmetric": True,
            "domain": "Entity",
            "range": "State",
            "description": "Entity is an acquaintance (loose connection)",
            "example": "Neighbor --[acquaintance_in]--> AcquaintanceState",
            "aliases": ["knows_in", "familiar_with_in"]
        },
        {
            "edge_type": "romantic_partner_in",
            "is_symmetric": True,
            "domain": "Entity",
            "range": "State",
            "description": "Entity is a romantic partner (dating)",
            "example": "Boyfriend --[romantic_partner_in]--> DatingState",
            "aliases": ["dating_in", "boyfriend_in", "girlfriend_in", "partner_in"]
        },
        {
            "edge_type": "ex_partner_in",
            "is_symmetric": True,
            "domain": "Entity",
            "range": "State",
            "description": "Entity is a former romantic partner",
            "example": "Ex --[ex_partner_in]--> PastRelationshipState",
            "aliases": ["ex_in", "former_partner_in"]
        },
        {
            "edge_type": "mentor_in",
            "inverse": "mentee_in",
            "domain": "Entity",
            "range": "State",
            "description": "Entity is the mentor in a mentorship",
            "example": "SeniorDev --[mentor_in]--> MentorshipState",
            "aliases": ["coach_in", "advisor_in", "guide_in"]
        },
        {
            "edge_type": "mentee_in",
            "inverse": "mentor_in",
            "domain": "Entity",
            "range": "State",
            "description": "Entity is the mentee in a mentorship",
            "example": "JuniorDev --[mentee_in]--> MentorshipState",
            "aliases": ["protege_in", "apprentice_in", "student_in"]
        },
        {
            "edge_type": "neighbor_in",
            "is_symmetric": True,
            "domain": "Entity",
            "range": "State",
            "description": "Entity is a neighbor",
            "example": "Person --[neighbor_in]--> NeighborhoodState",
            "aliases": ["lives_near_in", "next_door_in"]
        },
        {
            "edge_type": "roommate_in",
            "is_symmetric": True,
            "domain": "Entity",
            "range": "State",
            "description": "Entity is a roommate (shares living space)",
            "example": "Person --[roommate_in]--> RoommateState",
            "aliases": ["flatmate_in", "housemate_in", "cohabitant_in"]
        },
    ],
    
    # ==========================================
    # EVENT PARTICIPATION
    # ==========================================
    "event_participation": [
        {
            "edge_type": "attendee_of",
            "domain": "Entity",
            "range": "Event",
            "description": "Entity attended an event",
            "example": "Alex --[attendee_of]--> MeetingEvent",
            "aliases": ["participant_of", "attended", "was_at"]
        },
        {
            "edge_type": "organizer_of",
            "domain": "Entity",
            "range": "Event",
            "description": "Entity organized an event",
            "example": "Alice --[organizer_of]--> ConferenceEvent",
            "aliases": ["organized", "arranged", "coordinated"]
        },
        {
            "edge_type": "host_of",
            "domain": "Entity",
            "range": "Event",
            "description": "Entity hosted an event",
            "example": "Bob --[host_of]--> PartyEvent",
            "aliases": ["hosted", "threw", "held"]
        },
        {
            "edge_type": "speaker_at",
            "domain": "Entity",
            "range": "Event",
            "description": "Entity was a speaker at an event",
            "example": "Expert --[speaker_at]--> ConferenceEvent",
            "aliases": ["presented_at", "talked_at", "spoke_at"]
        },
        {
            "edge_type": "performer_at",
            "domain": "Entity",
            "range": "Event",
            "description": "Entity performed at an event",
            "example": "Band --[performer_at]--> ConcertEvent",
            "aliases": ["performed_at", "played_at", "entertained_at"]
        },
        {
            "edge_type": "guest_at",
            "domain": "Entity",
            "range": "Event",
            "description": "Entity was a guest at an event",
            "example": "Invitee --[guest_at]--> WeddingEvent",
            "aliases": ["invited_to", "guest_of"]
        },
        {
            "edge_type": "winner_of",
            "domain": "Entity",
            "range": "Event",
            "description": "Entity won or achieved something at an event",
            "example": "Champion --[winner_of]--> CompetitionEvent",
            "aliases": ["won", "achieved_at", "victor_of"]
        },
        {
            "edge_type": "competitor_in",
            "domain": "Entity",
            "range": "Event",
            "description": "Entity competed in an event",
            "example": "Athlete --[competitor_in]--> CompetitionEvent",
            "aliases": ["competed_in", "participant_in_competition"]
        },
        {
            "edge_type": "volunteer_at",
            "domain": "Entity",
            "range": "Event",
            "description": "Entity volunteered at an event",
            "example": "Helper --[volunteer_at]--> CharityEvent",
            "aliases": ["volunteered_at", "helped_at"]
        },
        {
            "edge_type": "sponsor_of",
            "domain": "Entity",
            "range": "Event",
            "description": "Entity sponsored an event",
            "example": "Company --[sponsor_of]--> ConferenceEvent",
            "aliases": ["sponsored", "funded", "backed"]
        },
    ],
    
    # ==========================================
    # PREFERENCE & INTEREST STATES
    # ==========================================
    "preference_state": [
        {
            "edge_type": "liker_in",
            "inverse": "liked_in",
            "domain": "Entity",
            "range": "State",
            "description": "Entity is the subject who likes something",
            "example": "Alex --[liker_in]--> PreferenceState(sushi)",
            "aliases": ["prefers_in", "enjoys_in", "loves_in"]
        },
        {
            "edge_type": "liked_in",
            "inverse": "liker_in",
            "domain": "Entity|Concept",
            "range": "State",
            "description": "Entity/concept is the object that is liked",
            "example": "Sushi --[liked_in]--> PreferenceState",
            "aliases": ["preferred_in", "enjoyed_in", "loved_in"]
        },
        {
            "edge_type": "disliker_in",
            "inverse": "disliked_in",
            "domain": "Entity",
            "range": "State",
            "description": "Entity is the subject who dislikes something",
            "example": "Alex --[disliker_in]--> DislikeState(mushrooms)",
            "aliases": ["hates_in", "avoids_in"]
        },
        {
            "edge_type": "disliked_in",
            "inverse": "disliker_in",
            "domain": "Entity|Concept",
            "range": "State",
            "description": "Entity/concept is the object that is disliked",
            "example": "Mushrooms --[disliked_in]--> DislikeState",
            "aliases": ["hated_in", "avoided_in"]
        },
        {
            "edge_type": "interested_party_in",
            "inverse": "interest_object_in",
            "domain": "Entity",
            "range": "State",
            "description": "Entity has an interest in something",
            "example": "Alex --[interested_party_in]--> InterestState(AI)",
            "aliases": ["curious_about_in", "fascinated_by_in"]
        },
        {
            "edge_type": "interest_object_in",
            "inverse": "interested_party_in",
            "domain": "Entity|Concept",
            "range": "State",
            "description": "Entity/concept is the object of interest",
            "example": "AI --[interest_object_in]--> InterestState",
            "aliases": ["subject_of_interest_in"]
        },
    ],
    
    # ==========================================
    # SKILL & ABILITY STATES
    # ==========================================
    "skill_state": [
        {
            "edge_type": "skilled_person_in",
            "inverse": "skill_object_in",
            "domain": "Entity",
            "range": "State",
            "description": "Entity has a skill",
            "example": "Alex --[skilled_person_in]--> SkillState(Python)",
            "aliases": ["expert_in", "proficient_in", "capable_in"]
        },
        {
            "edge_type": "skill_object_in",
            "inverse": "skilled_person_in",
            "domain": "Concept",
            "range": "State",
            "description": "Concept is the skill possessed",
            "example": "Python --[skill_object_in]--> SkillState",
            "aliases": ["expertise_in", "ability_in"]
        },
        {
            "edge_type": "learner_in",
            "inverse": "learning_subject_in",
            "domain": "Entity",
            "range": "State",
            "description": "Entity is learning something",
            "example": "Student --[learner_in]--> LearningState(Spanish)",
            "aliases": ["studying_in", "practicing_in"]
        },
        {
            "edge_type": "learning_subject_in",
            "inverse": "learner_in",
            "domain": "Concept",
            "range": "State",
            "description": "Concept is being learned",
            "example": "Spanish --[learning_subject_in]--> LearningState",
            "aliases": ["studied_in", "practiced_in"]
        },
        {
            "edge_type": "teacher_in",
            "inverse": "student_in",
            "domain": "Entity",
            "range": "State",
            "description": "Entity is teaching",
            "example": "Professor --[teacher_in]--> TeachingState",
            "aliases": ["instructor_in", "educator_in", "trainer_in"]
        },
        {
            "edge_type": "student_in",
            "inverse": "teacher_in",
            "domain": "Entity",
            "range": "State",
            "description": "Entity is being taught",
            "example": "Student --[student_in]--> TeachingState",
            "aliases": ["pupil_in", "trainee_in", "learner_in"]
        },
        {
            "edge_type": "taught_subject_in",
            "domain": "Concept",
            "range": "State",
            "description": "Concept/skill is being taught",
            "example": "Math --[taught_subject_in]--> TeachingState",
            "aliases": ["lesson_subject_in", "curriculum_in"]
        },
    ],
    
    # ==========================================
    # GOAL PARTICIPATION
    # ==========================================
    "goal_participation": [
        {
            "edge_type": "pursuer_of",
            "domain": "Entity",
            "range": "Goal",
            "description": "Entity is pursuing a goal",
            "example": "Alex --[pursuer_of]--> CareerGoal",
            "aliases": ["working_toward", "aiming_for", "striving_for"]
        },
        {
            "edge_type": "beneficiary_of",
            "domain": "Entity",
            "range": "Goal",
            "description": "Entity will benefit from goal achievement",
            "example": "Family --[beneficiary_of]--> FinancialGoal",
            "aliases": ["benefits_from", "gains_from"]
        },
        {
            "edge_type": "supporter_of",
            "domain": "Entity",
            "range": "Goal",
            "description": "Entity supports someone else's goal",
            "example": "Mentor --[supporter_of]--> CareerGoal",
            "aliases": ["helps_with", "encourages", "backs"]
        },
        {
            "edge_type": "obstacle_to",
            "domain": "Entity|State",
            "range": "Goal",
            "description": "Entity/state is an obstacle to goal",
            "example": "InjuryState --[obstacle_to]--> FitnessGoal",
            "aliases": ["blocks", "hinders", "prevents"]
        },
        {
            "edge_type": "milestone_of",
            "inverse": "has_milestone",
            "domain": "Event|State",
            "range": "Goal",
            "description": "Event/state is a milestone toward goal",
            "example": "GraduationEvent --[milestone_of]--> CareerGoal",
            "aliases": ["step_toward", "progress_in"]
        },
    ],
    
    # ==========================================
    # LOCATION & SPATIAL
    # ==========================================
    "location_relation": [
        {
            "edge_type": "located_at",
            "inverse": "location_of",
            "domain": "Event|State|Entity",
            "range": "Entity",
            "description": "Something is located at a place",
            "example": "MeetingEvent --[located_at]--> ConferenceRoom",
            "aliases": ["occurs_at", "happens_at", "situated_at"]
        },
        {
            "edge_type": "location_of",
            "inverse": "located_at",
            "domain": "Entity",
            "range": "Event|State|Entity",
            "description": "Place is the location of something",
            "example": "Office --[location_of]--> MeetingEvent",
            "aliases": ["hosts", "venue_for", "site_of"]
        },
        {
            "edge_type": "resident_of",
            "inverse": "residence_for",
            "domain": "Entity",
            "range": "Entity",
            "description": "Entity lives at a location",
            "example": "Alex --[resident_of]--> Maplewood",
            "aliases": ["lives_in", "resides_in", "dwells_in"]
        },
        {
            "edge_type": "residence_for",
            "inverse": "resident_of",
            "domain": "Entity",
            "range": "Entity",
            "description": "Location is home to entity",
            "example": "Maplewood --[residence_for]--> Alex",
            "aliases": ["home_to", "houses"]
        },
        {
            "edge_type": "born_in",
            "domain": "Entity",
            "range": "Entity",
            "description": "Entity was born at location",
            "example": "Alex --[born_in]--> the user's home country",
            "aliases": ["birthplace", "native_of"]
        },
        {
            "edge_type": "birthplace_of",
            "inverse": "born_in",
            "domain": "Entity",
            "range": "Entity",
            "description": "Location is birthplace of entity",
            "example": "the user's home country --[birthplace_of]--> Alex",
            "aliases": ["native_land_of"]
        },
    ],
    
    # ==========================================
    # TEMPORAL & LIFECYCLE
    # ==========================================
    "temporal_relation": [
        {
            "edge_type": "started_on",
            "domain": "State|Event|Goal",
            "range": "Concept",
            "description": "When something started (date/time)",
            "example": "EmploymentState --[started_on]--> 2020-01-15",
            "aliases": ["began_on", "initiated_on", "commenced_on"]
        },
        {
            "edge_type": "ended_on",
            "domain": "State|Event|Goal",
            "range": "Concept",
            "description": "When something ended (date/time)",
            "example": "EmploymentState --[ended_on]--> 2024-03-20",
            "aliases": ["finished_on", "concluded_on", "terminated_on"]
        },
        {
            "edge_type": "occurred_on",
            "domain": "Event",
            "range": "Concept",
            "description": "When an event occurred (date/time)",
            "example": "MeetingEvent --[occurred_on]--> 2024-01-15",
            "aliases": ["happened_on", "took_place_on"]
        },
        {
            "edge_type": "valid_during",
            "domain": "State",
            "range": "Concept",
            "description": "Time period when state was valid",
            "example": "OwnershipState --[valid_during]--> 2020-2024",
            "aliases": ["active_during", "in_effect_during"]
        },
        {
            "edge_type": "preceded_by",
            "inverse": "followed_by",
            "domain": "Event|State",
            "range": "Event|State",
            "description": "Something came before something else",
            "example": "Job2 --[preceded_by]--> Job1",
            "aliases": ["after", "succeeds"]
        },
        {
            "edge_type": "followed_by",
            "inverse": "preceded_by",
            "domain": "Event|State",
            "range": "Event|State",
            "description": "Something came after something else",
            "example": "Job1 --[followed_by]--> Job2",
            "aliases": ["before", "precedes"]
        },
    ],
    
    # ==========================================
    # PROPERTY & ATTRIBUTE STATES
    # ==========================================
    "property_state": [
        {
            "edge_type": "subject_of",
            "inverse": "property_of",
            "domain": "Entity",
            "range": "State",
            "description": "Entity has a property/attribute state",
            "example": "Alex --[subject_of]--> HeightProperty(6ft)",
            "aliases": ["has_property", "characterized_by"]
        },
        {
            "edge_type": "property_of",
            "inverse": "subject_of",
            "domain": "State",
            "range": "Entity",
            "description": "Property belongs to entity",
            "example": "HeightProperty --[property_of]--> Alex",
            "aliases": ["attribute_of", "characteristic_of"]
        },
        {
            "edge_type": "property_value_in",
            "domain": "Concept",
            "range": "State",
            "description": "Value of a property",
            "example": "6ft --[property_value_in]--> HeightProperty",
            "aliases": ["value_in", "measurement_in"]
        },
    ],
    
    # ==========================================
    # MEMBERSHIP & HIERARCHY
    # ==========================================
    "membership_relation": [
        {
            "edge_type": "member_of",
            "inverse": "has_member",
            "domain": "Entity",
            "range": "Entity",
            "description": "Entity is a member of a group/organization",
            "example": "Alex --[member_of]--> TechClub",
            "aliases": ["belongs_to", "part_of_group"]
        },
        {
            "edge_type": "has_member",
            "inverse": "member_of",
            "domain": "Entity",
            "range": "Entity",
            "description": "Group has a member",
            "example": "TechClub --[has_member]--> Alex",
            "aliases": ["includes", "contains_member"]
        },
        {
            "edge_type": "founder_of",
            "inverse": "founded_by",
            "domain": "Entity",
            "range": "Entity",
            "description": "Entity founded an organization",
            "example": "Entrepreneur --[founder_of]--> Startup",
            "aliases": ["created", "established", "started"]
        },
        {
            "edge_type": "founded_by",
            "inverse": "founder_of",
            "domain": "Entity",
            "range": "Entity",
            "description": "Organization was founded by entity",
            "example": "Startup --[founded_by]--> Entrepreneur",
            "aliases": ["created_by", "established_by"]
        },
        {
            "edge_type": "part_of",
            "inverse": "has_part",
            "domain": "Entity",
            "range": "Entity",
            "description": "Entity is a component of a larger entity",
            "example": "Department --[part_of]--> Company",
            "aliases": ["component_of", "subdivision_of"]
        },
        {
            "edge_type": "has_part",
            "inverse": "part_of",
            "domain": "Entity",
            "range": "Entity",
            "description": "Entity contains a component",
            "example": "Company --[has_part]--> Department",
            "aliases": ["includes", "contains"]
        },
    ],
    
    # ==========================================
    # COMMUNICATION & INTERACTION STATES
    # ==========================================
    "communication_state": [
        {
            "edge_type": "sender_in",
            "inverse": "recipient_in",
            "domain": "Entity",
            "range": "State",
            "description": "Entity sent a communication",
            "example": "Alex --[sender_in]--> EmailState",
            "aliases": ["author_in", "writer_in", "from_in"]
        },
        {
            "edge_type": "recipient_in",
            "inverse": "sender_in",
            "domain": "Entity",
            "range": "State",
            "description": "Entity received a communication",
            "example": "Alice --[recipient_in]--> EmailState",
            "aliases": ["receiver_in", "addressee_in", "to_in"]
        },
        {
            "edge_type": "subject_matter_in",
            "domain": "Concept",
            "range": "State",
            "description": "Topic of communication",
            "example": "ProjectUpdate --[subject_matter_in]--> EmailState",
            "aliases": ["topic_in", "about_in", "regarding_in"]
        },
    ],
    
    # ==========================================
    # HEALTH & MEDICAL STATES
    # ==========================================
    "health_state": [
        {
            "edge_type": "patient_in",
            "inverse": "practitioner_in",
            "domain": "Entity",
            "range": "State",
            "description": "Entity is the patient in a healthcare relationship",
            "example": "Alex --[patient_in]--> TreatmentState",
            "aliases": ["treated_in", "under_care_in"]
        },
        {
            "edge_type": "practitioner_in",
            "inverse": "patient_in",
            "domain": "Entity",
            "range": "State",
            "description": "Entity is the healthcare provider",
            "example": "Doctor --[practitioner_in]--> TreatmentState",
            "aliases": ["doctor_in", "physician_in", "therapist_in"]
        },
        {
            "edge_type": "condition_in",
            "domain": "Concept",
            "range": "State",
            "description": "Medical condition or diagnosis",
            "example": "Flu --[condition_in]--> IllnessState",
            "aliases": ["diagnosis_in", "ailment_in"]
        },
        {
            "edge_type": "treatment_in",
            "domain": "Concept",
            "range": "State",
            "description": "Treatment or therapy being applied",
            "example": "Antibiotics --[treatment_in]--> TreatmentState",
            "aliases": ["therapy_in", "medication_in"]
        },
    ],
    
    # ==========================================
    # FINANCIAL & TRANSACTION STATES
    # ==========================================
    "financial_state": [
        {
            "edge_type": "payer_in",
            "inverse": "payee_in",
            "domain": "Entity",
            "range": "State",
            "description": "Entity is paying/buyer in a transaction",
            "example": "Customer --[payer_in]--> PurchaseState",
            "aliases": ["buyer_in", "purchaser_in", "customer_in"]
        },
        {
            "edge_type": "payee_in",
            "inverse": "payer_in",
            "domain": "Entity",
            "range": "State",
            "description": "Entity is receiving payment/seller",
            "example": "Store --[payee_in]--> PurchaseState",
            "aliases": ["seller_in", "vendor_in", "merchant_in"]
        },
        {
            "edge_type": "purchased_item_in",
            "domain": "Entity|Concept",
            "range": "State",
            "description": "Item that was purchased",
            "example": "Laptop --[purchased_item_in]--> PurchaseState",
            "aliases": ["product_in", "item_in", "good_in"]
        },
        {
            "edge_type": "lender_in",
            "inverse": "borrower_in",
            "domain": "Entity",
            "range": "State",
            "description": "Entity lent money/item",
            "example": "Bank --[lender_in]--> LoanState",
            "aliases": ["creditor_in"]
        },
        {
            "edge_type": "borrower_in",
            "inverse": "lender_in",
            "domain": "Entity",
            "range": "State",
            "description": "Entity borrowed money/item",
            "example": "Customer --[borrower_in]--> LoanState",
            "aliases": ["debtor_in"]
        },
        {
            "edge_type": "investor_in",
            "domain": "Entity",
            "range": "State",
            "description": "Entity invested in something",
            "example": "VC --[investor_in]--> InvestmentState",
            "aliases": ["backer_in", "funder_in"]
        },
        {
            "edge_type": "investee_in",
            "inverse": "investor_in",
            "domain": "Entity",
            "range": "State",
            "description": "Entity received investment",
            "example": "Startup --[investee_in]--> InvestmentState",
            "aliases": ["funded_entity_in"]
        },
    ],
}


def flatten_edge_types():
    """Flatten the nested dictionary into a flat list of edge types."""
    all_edges = []
    for category, edges in EDGE_TYPES.items():
        for edge in edges:
            edge["category"] = category
            all_edges.append(edge)
    return all_edges


def seed_edge_types():
    """Seed the edge_canon table with canonical edge types."""
    from app.assistant.kg_core.kg_pipeline_old.standardization.standardization_manager import StandardizationManager
    
    session = get_session()
    manager = StandardizationManager(session)
    
    all_edges = flatten_edge_types()
    
    print(f"🌱 Seeding {len(all_edges)} canonical edge types...")
    
    created = 0
    skipped = 0
    
    for edge in all_edges:
        # Check if edge already exists
        existing = manager.find_edge_canonical_exact(
            domain_type=edge["domain"],
            range_type=edge["range"],
            predicate_text=edge["edge_type"]
        )
        
        if existing:
            skipped += 1
            continue
        
        # Create canonical edge
        canon_id, canon_pred = manager.create_edge_canonical(
            domain_type=edge["domain"],
            range_type=edge["range"],
            predicate_text=edge["edge_type"],
            source="seed_edge_types",
            context=edge["description"]
        )
        
        # Add aliases
        for alias in edge.get("aliases", []):
            manager.record_edge_alias(
                raw_text=alias,
                canon_id=canon_id,
                domain_type=edge["domain"],
                range_type=edge["range"],
                method="seed_alias",
                confidence=1.0,
                provenance={"source": "seed_edge_types"}
            )
        
        created += 1
        if created % 50 == 0:
            print(f"   Created {created} edge types...")
            session.commit()
    
    session.commit()
    session.close()
    
    print(f"✅ Seeding complete!")
    print(f"   Created: {created}")
    print(f"   Skipped (already exist): {skipped}")
    print(f"   Total: {len(all_edges)}")


if __name__ == "__main__":
    seed_edge_types()

