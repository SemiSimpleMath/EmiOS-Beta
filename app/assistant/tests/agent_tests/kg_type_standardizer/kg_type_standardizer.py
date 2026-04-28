from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.utils.pydantic_classes import Message


def main():
    # Create the agent
    agent = DI.agent_factory.create_agent("kg_maintenance::type_standardizer")
    if not agent:
        print("❌ Agent creation failed!")
        return

    msg_text = """   Cluster 1: ['favorite_hobby', 'has_hobby']
   Cluster 2: ['enjoys', 'enjoys_playing']
   Cluster 3: ['works_on', 'can_work_on', 'will_work_on', 'works_at', 'working_on', 'worked_on', 'works_as', 'works_for', 'works_in', 'worked_for']
   Cluster 4: ['has_role', 'holds_role']
   Cluster 5: ['focus_on', 'focuses_on']
   Cluster 6: ['is_popular_for', 'popular_for']
   Cluster 7: ['has_birth_year', 'has_birthday']
   Cluster 8: ['has_email', 'owns_email']
   Cluster 9: ['occurred_on', 'occurs_in', 'occurs_on']
   Cluster 10: ['has_event', 'had_event']
   Cluster 11: ['has_advisor', 'advisor_of', 'had_advisor']
   Cluster 12: ['shared_advisor_with', 'shares_advisor_with']
   Cluster 13: ['born_in', 'born_on']
   Cluster 14: ['member_of', 'is_member_of']
   Cluster 15: ['passionate_about', 'is_passionate_about']
   Cluster 16: ['has_condition', 'has_status']
   Cluster 17: ['lives_in', 'lived_in']
   Cluster 18: ['moved_to', 'moved_from']
   Cluster 19: ['met', 'met_at']
   Cluster 20: ['has_connection_with', 'connects_to']
   Cluster 21: ['participates_in', 'participated_in']
   Cluster 22: ['attended_high_school_with', 'attended_college_with', 'attended_high_school_in']
   Cluster 23: ['has_sister', 'has_brother']
   Cluster 24: ['younger_than', 'older_than']
   Cluster 25: ['child_of', 'parent_of']
   Cluster 26: ['considers_soulmate', 'considers_soul_mate']
   Cluster 27: ['has_best_friend', 'best_friend_of']
   Cluster 28: ['had_dog', 'has_dog']
   Cluster 29: ['has_sibling', 'sibling_of', 'siblings_with', 'sister_of', 'brother_of']
   Cluster 30: ['beloved_in', 'beloved_by']
   Cluster 31: ['owns', 'owned']
   Cluster 32: ['has_mobile_carrier', 'has_carrier']
   Cluster 33: ['has_spouse', 'has_wife']
   Cluster 34: ['writes_about', 'writes_for']
   Cluster 35: ['located_in', 'located_at']
   Cluster 36: ['biked_with', 'biked_from', 'biked_to']
   Cluster 37: ['less_prefers', 'prefers']
   Cluster 38: ['returns_to', 'returned_to']
   Cluster 39: ['drew_with', 'drew']
   Cluster 40: ['recorded_with', 'recorded_in']
   Cluster 41: ['held', 'holds']
   Cluster 42: ['is_fan_of', 'fan_of']
   Cluster 43: ['played_in', 'acted_in']
   Cluster 44: ['career_goal_at', 'career_goal']
   Cluster 45: ['practices', 'practices_on']
   Cluster 46: ['include', 'includes']
   Cluster 47: ['applies_for', 'applies_to']
   Cluster 48: ['enrolled_in', 'enrolls_in']
   Cluster 49: ['prepares_for', 'preparing_for', 'prepared_for', 'prepares']
   Cluster 50: ['refines_skills_in', 'refines_skills_with']
   Cluster 51: ['name_originates_from', 'originates_from']
   Cluster 52: ['appeared_in', 'first_appeared_in', 'was_in', 'appears_in', 'appeared_with']
   Cluster 53: ['helps', 'helped']
   Cluster 54: ['has_feature', 'has_features_in']
   Cluster 55: ['has_background_in', 'has_background_as']
   Cluster 56: ['wants_to_see', 'wants_to_watch']
   Cluster 57: ['not_involved_in', 'involved_in']
   Cluster 58: ['officemate_of', 'officemate_at']
   Cluster 59: ['used_for', 'used_in', 'used_on', 'used_to']
   Cluster 60: ['sent_by', 'sent_to', 'sent_email_to', 'sent_from']
   Cluster 61: ['includes_participant', 'has_participant', 'had_participant']
   Cluster 62: ['can_be_watched_on', 'can_be_watched_at']
   Cluster 63: ['favorite_dessert', 'favorite_cuisine']
   Cluster 64: ['intends_to_use', 'intends_to_incorporate', 'intends_to_integrate']
   Cluster 65: ['to_be_incorporated_in', 'incorporated_in']
   Cluster 66: ['wants_to_use', 'wants_to_access']
   Cluster 67: ['planned_to_use', 'plans_to_use']
   Cluster 68: ['integrates_into', 'integrate_into', 'integrates_with']
   Cluster 69: ['suggested_using', 'considered_using']
   Cluster 70: ['likely_member_of', 'is_likely_member_of']
   Cluster 71: ['familiar_with', 'unfamiliar_with']
   Cluster 72: ['aims_to_predict', 'does_not_aim_to_predict', 'aims_for']
   Cluster 73: ['has_experience_playing', 'has_experience_with']
   Cluster 74: ['aims_for_competition_with', 'aims_to_compete_with', 'aims_to_rival']
   Cluster 75: ['intends_to_open_source', 'plans_to_open_source']
   Cluster 76: ['has_level_in', 'has_level']
   Cluster 77: ['clearing', 'cleared']
   Cluster 78: ['has_skill', 'has_skill_level']
   Cluster 79: ['inquired_about', 'asked_about']
   Cluster 80: ['targets', 'targeted']
   Cluster 81: ['discussed_using', 'discussed_with']
   Cluster 82: ['allows_decryption_by', 'allows_decrypt']
   Cluster 83: ['born_in_year', 'born_in_month']
   Cluster 84: ['close_in_age_with', 'closer_in_age_to']
   Cluster 85: ['offered_at', 'offered_by']
   Cluster 86: ['covers', 'cover']
   Cluster 87: ['traveling_to', 'will_travel_to']
   Cluster 88: ['can_assign_to', 'assigned_for', 'assigned_to']
   Cluster 89: ['is_strong_in', 'strong_in']
   Cluster 90: ['can_assign', 'can_be_assigned']
   Cluster 91: ['studies', 'studies_for']
   Cluster 92: ['aims_for_independent_activities', 'wants_independent_activities_for']
   """



    print(f"\n=== INPUT MESSAGE ===\n{msg_text}")
    msg = Message(agent_input={"text":msg_text})
    result = agent.action_handler(msg)
    print(f"=== PARSER OUTPUT ===\n{result}\n")


if __name__ == "__main__":
    main()