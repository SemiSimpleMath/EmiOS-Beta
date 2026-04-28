from app.assistant.database.db_instance import db

class LLMConfig(db.Model):
    __tablename__ = 'llm_config'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user_login.id'), nullable=False)
    model_name = db.Column(db.String(255), nullable=False)

