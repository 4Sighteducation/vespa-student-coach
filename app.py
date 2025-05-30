import os
from flask import Flask, jsonify, request
from flask_cors import CORS
from dotenv import load_dotenv
import logging

# Load environment variables from .env file (optional, Heroku uses config vars)
load_dotenv()

app = Flask(__name__)

# --- CORS Configuration ---
# Allow requests ONLY from your Knack domain for security.
CORS(app, resources={r"/api/*": {"origins": "https://vespaacademy.knack.com"}})

# --- Logging Configuration ---
# Basic logging setup
if not app.debug:
    app.logger.setLevel(logging.INFO)
    handler = logging.StreamHandler()
    handler.setLevel(logging.INFO)
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    handler.setFormatter(formatter)
    if not app.logger.handlers:
        app.logger.addHandler(handler)

app.logger.info("Flask Student Coach App Initializing...")

# --- Environment Variables (placeholders, to be set in Heroku) ---
KNACK_APP_ID = os.getenv('KNACK_APP_ID')
KNACK_API_KEY = os.getenv('KNACK_API_KEY')
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')

if not KNACK_APP_ID or not KNACK_API_KEY:
    app.logger.warning("KNACK_APP_ID or KNACK_API_KEY is not set. Knack integration will fail.")
if not OPENAI_API_KEY:
    app.logger.warning("OPENAI_API_KEY is not set. OpenAI integration will fail.")

# --- Placeholder API Endpoints ---_API_KEY

@app.route('/api/v1/student_coaching_data', methods=['POST', 'OPTIONS'])
def student_coaching_data():
    app.logger.info(f"Received request for /api/v1/student_coaching_data. Method: {request.method}")
    if request.method == 'OPTIONS': # Handle preflight request for CORS
        return _build_cors_preflight_response()
    if request.method == 'POST':
        # In a real scenario, you would fetch and process student data here.
        # For now, returning dummy data.
        data = request.get_json()
        student_object3_id = data.get('student_object3_id', 'Unknown Student ID')
        app.logger.info(f"Request body for student_coaching_data: {data}")
        
        dummy_response = {
            "student_name": "Test Student",
            "student_level": "Level 3",
            "current_cycle": 1,
            "vespa_profile": { # Placeholder, structure will be more detailed
                "Vision": {"score_1_to_10": 7, "score_profile_text": "Medium"},
                "Effort": {"score_1_to_10": 8, "score_profile_text": "High"},
                "Systems": {"score_1_to_10": 6, "score_profile_text": "Medium"},
                "Practice": {"score_1_to_10": 5, "score_profile_text": "Low"},
                "Attitude": {"score_1_to_10": 9, "score_profile_text": "High"}
            },
            "academic_profile_summary": [
                {"subject": "Placeholder Subject 1", "currentGrade": "A", "targetGrade": "A*", "effortGrade": "1", "normalized_qualification_type": "A Level", "currentGradePoints": 120, "standardMegPoints": 110},
            ],
            "student_reflections_and_goals": {"rrc1_comment": "Working hard", "goal1": "Get good grades"},
            "object29_question_highlights": {"top_3": [], "bottom_3": []},
            "llm_generated_insights": {
                "student_overview_summary": f"This is a placeholder AI snapshot for student {student_object3_id}. Coaching data backend is active but not fully implemented.",
                "chart_comparative_insights": "Placeholder chart insights.",
                "most_important_coaching_questions": ["What are you most proud of?"],
                "student_comment_analysis": "Placeholder comment analysis.",
                "suggested_student_goals": ["Set a placeholder goal."],
                "academic_benchmark_analysis": "Placeholder academic benchmark analysis.",
                "questionnaire_interpretation_and_reflection_summary": "Placeholder questionnaire interpretation."
            },
            "all_scored_questionnaire_statements": [], # For pie chart
            "school_vespa_averages": None # Placeholder
        }
        return jsonify(dummy_response), 200

@app.route('/api/v1/chat_turn', methods=['POST', 'OPTIONS'])
def chat_turn():
    app.logger.info(f"Received request for /api/v1/chat_turn. Method: {request.method}")
    if request.method == 'OPTIONS':
        return _build_cors_preflight_response()
    if request.method == 'POST':
        data = request.get_json()
        student_knack_id = data.get('student_knack_id', 'Unknown Student')
        current_user_message = data.get('current_user_message', 'No message')
        app.logger.info(f"Chat turn for student {student_knack_id}. Message: {current_user_message}")
        
        # Dummy AI response
        dummy_response = {
            "ai_response": f"Hello {student_knack_id}! You said: '{current_user_message}'. I am a placeholder AI. Full chat functionality coming soon!",
            "suggested_activities_in_chat": [],
            "ai_message_knack_id": "dummy_chat_id_123" # Placeholder for potential future use
        }
        return jsonify(dummy_response), 200

# Add other placeholder endpoints as needed by vespa-student-coach.js, e.g., for chat history
@app.route('/api/v1/chat_history', methods=['POST', 'OPTIONS'])
def chat_history():
    app.logger.info(f"Received request for /api/v1/chat_history. Method: {request.method}")
    if request.method == 'OPTIONS':
        return _build_cors_preflight_response()
    if request.method == 'POST':
        data = request.get_json()
        student_knack_id = data.get('student_knack_id', 'Unknown Student')
        app.logger.info(f"Chat history request for student {student_knack_id}")
        dummy_response = {
            "chat_history": [
                {"role": "assistant", "content": "Welcome to the placeholder chat history!"}
            ],
            "total_count": 1,
            "liked_count": 0,
            "summary": "This is a placeholder summary."
        }
        return jsonify(dummy_response), 200

# Helper function for CORS preflight responses
def _build_cors_preflight_response():
    response = jsonify(success=True)
    # These headers are important for CORS preflight
    response.headers.add("Access-Control-Allow-Origin", "https://vespaacademy.knack.com")
    response.headers.add('Access-Control-Allow-Headers', "Content-Type,Authorization")
    response.headers.add('Access-Control-Allow-Methods', "GET,PUT,POST,DELETE,OPTIONS")
    app.logger.info("Built CORS preflight response.")
    return response

# Basic health check endpoint
@app.route('/', methods=['GET'])
@app.route('/health', methods=['GET'])
def health_check():
    app.logger.info("Health check endpoint was hit.")
    return jsonify({"status": "Healthy", "message": "Student Coach Backend is running!"}), 200

if __name__ == '__main__':
    # For local development. Heroku uses Procfile.
    port = int(os.environ.get('PORT', 5002)) # Use a different port than tutor coach if running locally
    app.run(debug=True, port=port, host='0.0.0.0') 