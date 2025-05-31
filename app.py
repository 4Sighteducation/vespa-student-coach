import os
import json
from flask import Flask, jsonify, request
from flask_cors import CORS
from dotenv import load_dotenv
import logging
import requests # For Knack API calls
import time # For cache expiry
from datetime import datetime # For timestamp parsing
import openai # For LLM integration

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

# Knack base URL for API calls - good to define once
KNACK_API_BASE_URL = "https://api.knack.com/v1/objects"

if not KNACK_APP_ID or not KNACK_API_KEY:
    app.logger.warning("KNACK_APP_ID or KNACK_API_KEY is not set. Knack integration will fail.")
if not OPENAI_API_KEY:
    app.logger.warning("OPENAI_API_KEY is not set. OpenAI integration will fail.")

# --- Load Knowledge Bases (Copied from Tutor app.py, adapt paths if needed) ---
def load_json_file(file_path):
    try:
        # Assuming KB files are in a 'knowledge_base' subdirectory relative to this app.py
        current_dir = os.path.dirname(os.path.abspath(__file__))
        full_path = os.path.join(current_dir, 'knowledge_base', file_path)
        full_path = os.path.normpath(full_path)
        app.logger.info(f"Attempting to load JSON KB: {full_path}")
        with open(full_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        # Check if data is in Knack 'records' format for some files
        if isinstance(data, dict) and 'records' in data and isinstance(data['records'], list) and file_path in ['reporttext.json']:
            app.logger.info(f"Extracted {len(data['records'])} records from {file_path}")
            return data['records']
        app.logger.info(f"Loaded {file_path} (data type: {type(data)})")
        return data
    except FileNotFoundError:
        app.logger.error(f"Knowledge base file not found: {file_path} (looked in {full_path})")
    except json.JSONDecodeError:
        app.logger.error(f"Error decoding JSON from file: {file_path}")
    except Exception as e:
        app.logger.error(f"Error loading JSON file {file_path}: {e}")
    return None

# Load relevant KBs - adjust file names/paths as per your student coach's KB structure
psychometric_question_details_kb = load_json_file('psychometric_question_details.json')
report_text_kb = load_json_file('reporttext.json') # Object_33 content
grade_points_mapping_kb = load_json_file('grade_to_points_mapping.json')
# Add ALPS band KBs if academic benchmarks are to be calculated in detail
alps_bands_aLevel_75_kb = load_json_file('alpsBands_aLevel_75.json') # Example for standard MEG
# ... load other ALPS KBs as needed (60th, 90th, 100th, BTEC, etc.)

# --- Knack API Helper Functions (Adapted from Tutor app.py) ---
def get_knack_record(object_key, record_id=None, filters=None, page=1, rows_per_page=1000):
    if not KNACK_APP_ID or not KNACK_API_KEY:
        app.logger.error("Knack App ID or API Key is missing for get_knack_record.")
        return None
    headers = {
        'X-Knack-Application-Id': KNACK_APP_ID,
        'X-Knack-REST-API-Key': KNACK_API_KEY,
        'Content-Type': 'application/json'
    }
    params = {'page': page, 'rows_per_page': rows_per_page}
    if filters:
        params['filters'] = json.dumps(filters)

    url_path = f"/{object_key}/records"
    if record_id:
        url_path = f"/{object_key}/records/{record_id}"
        current_params = {} # No params for specific ID fetch usually
    else:
        current_params = params
    
    full_url = f"{KNACK_API_BASE_URL}{url_path}"
    app.logger.info(f"Knack API call: URL={full_url}, Params={current_params}")

    try:
        response = requests.get(full_url, headers=headers, params=current_params)
        response.raise_for_status()
        data = response.json()
        app.logger.info(f"Knack API success for {object_key}. Records: {len(data.get('records', [])) if not record_id else '1 (specific ID)'}")
        return data
    except requests.exceptions.HTTPError as e:
        app.logger.error(f"HTTP error fetching Knack data ({object_key}): {e}. Response: {response.content if response else 'No response object'}")
    except requests.exceptions.RequestException as e:
        app.logger.error(f"Request exception fetching Knack data ({object_key}): {e}")
    except json.JSONDecodeError:
        app.logger.error(f"JSON decode error for Knack response ({object_key}). Response text: {response.text if response else 'No response object'}")
    return None

# --- Helper function to extract qualification details (ported from tutorapp.py) ---
def extract_qual_details(exam_type_str, normalized_qual_type, app_logger_instance):
    """Extracts specific details (like year, size) from an exam_type_str based on its normalized type."""
    if not exam_type_str or not normalized_qual_type:
        app_logger_instance.debug(f"extract_qual_details: exam_type_str ('{exam_type_str}') or normalized_qual_type ('{normalized_qual_type}') is missing.")
        return None
    
    lower_exam_type = str(exam_type_str).lower()
    details = {}

    if normalized_qual_type == "IB HL":
        details['ib_level'] = "HL"
        return details
    if normalized_qual_type == "IB SL":
        details['ib_level'] = "SL"
        return details

    if "BTEC" in normalized_qual_type:
        if "2010" in lower_exam_type: details['year'] = "2010"
        elif "2016" in lower_exam_type: details['year'] = "2016"
        else:
            details['year'] = "2016" # Default BTEC year if not specified
            app_logger_instance.info(f"BTEC year not specified in '{exam_type_str}', defaulting to {details['year']} for MEG lookup.")
        
        # Determine BTEC size based on normalized type
        if normalized_qual_type == "BTEC Level 3 Extended Diploma": details['size'] = "EXTDIP"
        elif normalized_qual_type == "BTEC Level 3 Diploma": details['size'] = "DIP"
        elif normalized_qual_type == "BTEC Level 3 Subsidiary Diploma": details['size'] = "SUBDIP"
        elif normalized_qual_type == "BTEC Level 3 Extended Certificate": # This is often the default "BTEC Level 3"
            # Size for "Extended Certificate" can depend on the year for some ALPS tables
            if details['year'] == "2010":
                # For 2010, an "Extended Certificate" might be referred to as just "Certificate" in ALPS bands
                details['size'] = "CERT" 
            else: # For 2016, it's usually "EXTCERT"
                details['size'] = "EXTCERT"
        elif "foundation diploma" in lower_exam_type : details['size'] = "FOUNDDIP"
        elif "90 credit diploma" in lower_exam_type or "90cr" in lower_exam_type : details['size'] = "NINETY_CR"
        # Add other BTEC size mappings if necessary based on your ALPS band JSON keys
        
        if not details.get('size'):
             app_logger_instance.warning(f"Could not determine BTEC size for MEG key from '{exam_type_str}' (Normalized: '{normalized_qual_type}'). MEG lookup might fail.")
        return details

    if "Pre-U" in normalized_qual_type:
        if normalized_qual_type == "Pre-U Principal Subject": details['pre_u_type'] = "FULL"
        elif normalized_qual_type == "Pre-U Short Course": details['pre_u_type'] = "SC"
        return details

    if "WJEC" in normalized_qual_type:
        if normalized_qual_type == "WJEC Level 3 Diploma": details['wjec_size'] = "DIP"
        elif normalized_qual_type == "WJEC Level 3 Certificate": details['wjec_size'] = "CERT"
        else: # Default if not clearly diploma or certificate but identified as WJEC
            details['wjec_size'] = "CERT" 
            app_logger_instance.info(f"WJEC size not clearly diploma/certificate from '{normalized_qual_type}', defaulting to CERT for MEG lookup.")
        return details
    
    # No specific details needed for A-Level, AS-Level, UAL, CACHE for this function as per tutorapp.py structure
    # If they were needed (e.g. UAL Diploma vs ExtDip affecting MEG key), they would be added here.
    app_logger_instance.debug(f"No specific details extracted for '{normalized_qual_type}' from '{exam_type_str}'.")
    return None # Return None if no specific details are extracted for the given type

# --- Student Data Specific Fetching Functions ---

def get_student_user_details(student_object3_id):
    "Fetches Object_3 record for the student."
    if not student_object3_id: return None
    return get_knack_record("object_3", record_id=student_object3_id)

def get_student_object10_record(student_email):
    "Fetches student's Object_10 (VESPA Results) record using their email."
    if not student_email: return None
    filters = [{'field': 'field_197', 'operator': 'is', 'value': student_email}] # field_197 is student email in Object_10
    response = get_knack_record("object_10", filters=filters)
    if response and response.get('records'):
        if len(response['records']) > 1:
            app.logger.warning(f"Multiple Object_10 records found for email {student_email}. Using the first one.")
        return response['records'][0]
    app.logger.warning(f"No Object_10 record found for email {student_email}.")
    return None

def get_student_object29_questionnaire_data(object10_id, cycle_number):
    "Fetches Object_29 (Questionnaire) data for a given Object_10 ID and cycle."
    if not object10_id or cycle_number is None: return None
    app.logger.info(f"Fetching Object_29 questionnaire data for Object_10 ID: {object10_id}, Cycle: {cycle_number}")
    filters = [
        {'field': 'field_792', 'operator': 'is', 'value': object10_id}, # Connection to Object_10
        {'field': 'field_863_raw', 'operator': 'is', 'value': str(cycle_number)} # Cycle number
    ]
    response = get_knack_record("object_29", filters=filters)
    if response and response.get('records'):
        if len(response['records']) > 1:
            app.logger.warning(f"Multiple Object_29 records found for Object_10 ID {object10_id}, cycle {cycle_number}. Using the first one.")
        return response['records'][0] # Assuming one Object_29 per student per cycle
    app.logger.warning(f"No Object_29 data found for Object_10 ID {object10_id}, Cycle {cycle_number}.")
    return None

# --- Ported Academic Profile Functions from tutorapp.py ---

# Helper function to parse subjects from a given academic_profile_record (ported from tutorapp.py)
def parse_subjects_from_profile_record(academic_profile_record, app_logger_instance):
    if not academic_profile_record:
        app_logger_instance.error("parse_subjects_from_profile_record called with no record.")
        return [] 

    app_logger_instance.info(f"Parsing subjects for Object_112 record ID: {academic_profile_record.get('id')}. Record (first 500 chars): {str(academic_profile_record)[:500]}")
    subjects_summary = []
    # Subject fields are field_3080 (Sub1) to field_3094 (Sub15) in tutorapp, assuming same for student view if Obj112 is shared
    for i in range(1, 16):
        field_id_subject_json = f"field_30{79+i}" # field_3080 to field_3094
        subject_json_str = academic_profile_record.get(field_id_subject_json)
        if subject_json_str is None:
            subject_json_str = academic_profile_record.get(f"{field_id_subject_json}_raw")

        app_logger_instance.debug(f"For Obj112 ID {academic_profile_record.get('id')}, field {field_id_subject_json}: Data type: {type(subject_json_str)}, Content (brief): '{str(subject_json_str)[:100]}...'")
        
        if subject_json_str and isinstance(subject_json_str, str) and subject_json_str.strip().startswith('{'):
            app_logger_instance.info(f"Attempting to parse JSON for {field_id_subject_json}: '{subject_json_str[:200]}...'")
            try:
                subject_data = json.loads(subject_json_str)
                app_logger_instance.info(f"Parsed subject_data for {field_id_subject_json}: {subject_data}")
                summary_entry = {
                    "subject": subject_data.get("subject") or subject_data.get("subject_name") or subject_data.get("subjectName") or subject_data.get("name", "N/A"),
                    "currentGrade": subject_data.get("currentGrade") or subject_data.get("current_grade") or subject_data.get("cg") or subject_data.get("currentgrade", "N/A"),
                    "targetGrade": subject_data.get("targetGrade") or subject_data.get("target_grade") or subject_data.get("tg") or subject_data.get("targetgrade", "N/A"),
                    "effortGrade": subject_data.get("effortGrade") or subject_data.get("effort_grade") or subject_data.get("eg") or subject_data.get("effortgrade", "N/A"),
                    "examType": subject_data.get("examType") or subject_data.get("exam_type") or subject_data.get("qualificationType", "N/A")
                }
                if summary_entry["subject"] != "N/A" and summary_entry["subject"] is not None:
                    subjects_summary.append(summary_entry)
                    app_logger_instance.debug(f"Added subject: {summary_entry['subject']}")
                else:
                    app_logger_instance.info(f"Skipped adding subject for {field_id_subject_json} as subject name was invalid or N/A. Parsed data: {subject_data}")
            except json.JSONDecodeError as e:
                app_logger_instance.warning(f"JSONDecodeError for {field_id_subject_json}: {e}. Content: '{subject_json_str[:100]}...'")
        elif subject_json_str:
            app_logger_instance.info(f"Field {field_id_subject_json} was not empty but not a valid JSON string start: '{subject_json_str[:100]}...'")

    if not subjects_summary:
        app_logger_instance.info(f"No valid subject JSONs parsed from Object_112 record {academic_profile_record.get('id')}. Returning default message list.")
        return [{"subject": "No academic subjects parsed from profile.", "currentGrade": "N/A", "targetGrade": "N/A", "effortGrade": "N/A", "examType": "N/A"}]
    
    app_logger_instance.info(f"Successfully parsed {len(subjects_summary)} subjects from Object_112 record {academic_profile_record.get('id')}.")
    return subjects_summary

# Function to fetch Academic Profile (Object_112) - (ported from tutorapp.py)
def get_academic_profile(actual_student_obj3_id, student_name_for_fallback, app_logger_instance, student_obj10_id_log_ref="N/A"):
    app_logger_instance.info(f"Starting academic profile fetch. Target Student's Object_3 ID: '{actual_student_obj3_id}', Fallback Name: '{student_name_for_fallback}', Original Obj10 ID for logging: {student_obj10_id_log_ref}.")
    
    academic_profile_record = None
    subjects_summary = []

    # Attempt 1: Fetch Object_112 using actual_student_obj3_id against Object_112.field_3064 (UserId - Short Text field)
    if actual_student_obj3_id:
        app_logger_instance.info(f"Attempt 1: Fetching Object_112 where field_3064 (UserId Text) is '{actual_student_obj3_id}'.")
        filters_obj112_via_field3064 = [{'field': 'field_3064', 'operator': 'is', 'value': actual_student_obj3_id}]
        obj112_response_attempt1 = get_knack_record("object_112", filters=filters_obj112_via_field3064)

        temp_profiles_list_attempt1 = []
        if obj112_response_attempt1 and isinstance(obj112_response_attempt1, dict) and \
           'records' in obj112_response_attempt1 and isinstance(obj112_response_attempt1['records'], list):
            temp_profiles_list_attempt1 = obj112_response_attempt1['records']
            app_logger_instance.info(f"Attempt 1: Found {len(temp_profiles_list_attempt1)} candidate profiles via field_3064.")
        else:
            app_logger_instance.info(f"Attempt 1: Knack response for field_3064 query was not in expected format or no records. Response: {str(obj112_response_attempt1)[:200]}")

        if temp_profiles_list_attempt1: 
            if isinstance(temp_profiles_list_attempt1[0], dict):
                academic_profile_record = temp_profiles_list_attempt1[0]
                app_logger_instance.info(f"Attempt 1 SUCCESS: Found Object_112 record ID {academic_profile_record.get('id')} using field_3064 with Obj3 ID '{actual_student_obj3_id}'. Profile Name: {academic_profile_record.get('field_3066')}")
                subjects_summary = parse_subjects_from_profile_record(academic_profile_record, app_logger_instance)
                if not subjects_summary or (len(subjects_summary) == 1 and subjects_summary[0]["subject"].startswith("No academic subjects")):
                    app_logger_instance.info(f"Object_112 ID {academic_profile_record.get('id')} (via field_3064) yielded no valid subjects. Will try other methods.")
                    academic_profile_record = None 
                else:
                    app_logger_instance.info(f"Object_112 ID {academic_profile_record.get('id')} (via field_3064) has valid subjects. Using this profile.")
                    return {"subjects": subjects_summary, "profile_record": academic_profile_record}
            else:
                app_logger_instance.warning(f"Attempt 1: First item in profiles_via_field3064 is not a dict: {type(temp_profiles_list_attempt1[0])}")
        else:
            app_logger_instance.info(f"Attempt 1 FAILED (empty list): No Object_112 profile found where field_3064 (UserId Text) is '{actual_student_obj3_id}'.")

    # Attempt 2: Fetch Object_112 using actual_student_obj3_id against Object_112.field_3070 (Account Connection field)
    if not academic_profile_record and actual_student_obj3_id: 
        app_logger_instance.info(f"Attempt 2: Fetching Object_112 where field_3070 (Account Connection) is '{actual_student_obj3_id}'.")
        filters_obj112_via_field3070 = [{'field': 'field_3070_raw', 'operator': 'is', 'value': actual_student_obj3_id}]
        obj112_response_attempt2 = get_knack_record("object_112", filters=filters_obj112_via_field3070)
        
        temp_profiles_list_attempt2 = []
        if not (obj112_response_attempt2 and isinstance(obj112_response_attempt2, dict) and 'records' in obj112_response_attempt2 and isinstance(obj112_response_attempt2['records'], list) and obj112_response_attempt2['records']):
            app_logger_instance.info(f"Attempt 2 (field_3070_raw): No records or unexpected format. Trying 'field_3070' (non-raw). Response: {str(obj112_response_attempt2)[:200]}" )
            filters_obj112_via_field3070_alt = [{'field': 'field_3070', 'operator': 'is', 'value': actual_student_obj3_id}]
            obj112_response_attempt2 = get_knack_record("object_112", filters=filters_obj112_via_field3070_alt)

        if obj112_response_attempt2 and isinstance(obj112_response_attempt2, dict) and \
           'records' in obj112_response_attempt2 and isinstance(obj112_response_attempt2['records'], list):
            temp_profiles_list_attempt2 = obj112_response_attempt2['records']
            app_logger_instance.info(f"Attempt 2: Found {len(temp_profiles_list_attempt2)} candidate profiles via field_3070 logic.")

        if temp_profiles_list_attempt2: 
            if isinstance(temp_profiles_list_attempt2[0], dict):
                academic_profile_record = temp_profiles_list_attempt2[0]
                app_logger_instance.info(f"Attempt 2 SUCCESS: Found Object_112 record ID {academic_profile_record.get('id')} using field_3070 (Account Connection) with Obj3 ID '{actual_student_obj3_id}'. Profile Name: {academic_profile_record.get('field_3066')}")
                subjects_summary = parse_subjects_from_profile_record(academic_profile_record, app_logger_instance)
                if not subjects_summary or (len(subjects_summary) == 1 and subjects_summary[0]["subject"].startswith("No academic subjects")):
                    app_logger_instance.info(f"Object_112 ID {academic_profile_record.get('id')} (via field_3070) yielded no valid subjects. Will try name fallback.")
                    academic_profile_record = None 
                else:
                    app_logger_instance.info(f"Object_112 ID {academic_profile_record.get('id')} (via field_3070) has valid subjects. Using this profile.")
                    return {"subjects": subjects_summary, "profile_record": academic_profile_record}
            else:
                app_logger_instance.warning(f"Attempt 2: First item in profiles_via_field3070 is not a dict: {type(temp_profiles_list_attempt2[0])}")
        else:
            app_logger_instance.info(f"Attempt 2 FAILED (empty list): No Object_112 profile found where field_3070 (Account Connection) is '{actual_student_obj3_id}'.")

    # Attempt 3: Fallback to fetch by student name
    if not academic_profile_record and student_name_for_fallback and student_name_for_fallback != "N/A":
        app_logger_instance.info(f"Attempt 3: Fallback search for Object_112 by student name ('{student_name_for_fallback}') via field_3066.")
        filters_object112_name = [{'field': 'field_3066', 'operator': 'is', 'value': student_name_for_fallback}]
        obj112_response_attempt3 = get_knack_record("object_112", filters=filters_object112_name)
        
        temp_profiles_list_attempt3 = []
        if obj112_response_attempt3 and isinstance(obj112_response_attempt3, dict) and \
           'records' in obj112_response_attempt3 and isinstance(obj112_response_attempt3['records'], list):
            temp_profiles_list_attempt3 = obj112_response_attempt3['records']
            app_logger_instance.info(f"Attempt 3: Found {len(temp_profiles_list_attempt3)} candidate profiles via name fallback.")

        if temp_profiles_list_attempt3: 
            if isinstance(temp_profiles_list_attempt3[0], dict):
                academic_profile_record = temp_profiles_list_attempt3[0]
                app_logger_instance.info(f"Attempt 3 SUCCESS: Found Object_112 record ID {academic_profile_record.get('id')} via NAME fallback ('{student_name_for_fallback}'). Profile Name: {academic_profile_record.get('field_3066')}")
                subjects_summary = parse_subjects_from_profile_record(academic_profile_record, app_logger_instance)
                if not subjects_summary or (len(subjects_summary) == 1 and subjects_summary[0]["subject"].startswith("No academic subjects")):
                    app_logger_instance.info(f"Object_112 ID {academic_profile_record.get('id')} (via name fallback) yielded no valid subjects.")
                    # Fall through to default return
                else:
                    app_logger_instance.info(f"Object_112 ID {academic_profile_record.get('id')} (via name fallback) has valid subjects. Using this profile.")
                    return {"subjects": subjects_summary, "profile_record": academic_profile_record}
            else:
                app_logger_instance.warning(f"Attempt 3: First item in homepage_profiles_name_search is not a dict: {type(temp_profiles_list_attempt3[0])}")
        else:
            app_logger_instance.warning(f"Attempt 3 FAILED (empty list): Fallback search: No Object_112 found for student name: '{student_name_for_fallback}'.")
    
    app_logger_instance.warning(f"All attempts to fetch Object_112 failed (Student's Obj3 ID: '{actual_student_obj3_id}', Fallback name: '{student_name_for_fallback}').")
    default_subjects = [{"subject": "Academic profile not found by any method.", "currentGrade": "N/A", "targetGrade": "N/A", "effortGrade": "N/A", "examType": "N/A"}]
    return {"subjects": default_subjects, "profile_record": None}

# --- Data Processing Helper (Simplified for now) ---
def get_score_profile_text(score_value):
    if score_value is None: return "N/A"
    try:
        score = float(score_value)
        if score >= 8: return "High"
        if score >= 6: return "Medium"
        if score >= 4: return "Low"
        if score >= 0: return "Very Low"
        return "N/A"
    except (ValueError, TypeError):
        return "N/A"

# --- NEW: Add comprehensive data processing functions ---

def get_all_knack_records(object_key, filters=None, max_pages=20):
    """Fetches all records from a Knack object using pagination."""
    all_records = []
    current_page = 1
    total_pages = 1

    app.logger.info(f"Starting paginated fetch for {object_key} with filters: {filters}")

    while current_page <= total_pages and current_page <= max_pages:
        app.logger.info(f"Fetching page {current_page} for {object_key}...")
        response_data = get_knack_record(object_key, filters=filters, page=current_page, rows_per_page=1000)
        
        if response_data and isinstance(response_data, dict):
            records_on_page = response_data.get('records', [])
            
            if isinstance(records_on_page, list):
                all_records.extend(records_on_page)
                app.logger.info(f"Fetched {len(records_on_page)} records from page {current_page} for {object_key}. Total so far: {len(all_records)}.")
            else:
                app.logger.warning(f"'records' key in response_data for {object_key} page {current_page} is not a list. Type: {type(records_on_page)}. Stopping pagination.")
                break
            
            new_total_pages = response_data.get('total_pages')
            if new_total_pages is not None:
                try:
                    total_pages = int(new_total_pages)
                    if current_page == 1:
                        app.logger.info(f"Total pages for {object_key} identified from API: {total_pages}")
                except (ValueError, TypeError):
                    app.logger.warning(f"Could not parse 'total_pages' ('{new_total_pages}') from response for {object_key} on page {current_page}.")
            
            if not records_on_page or len(records_on_page) < 1000 or current_page >= total_pages:
                app.logger.info(f"Last page likely reached for {object_key} on page {current_page}.")
                break
            current_page += 1
        else:
            app.logger.warning(f"No response_data or unexpected format on page {current_page} for {object_key}. Stopping pagination.")
            break
            
    app.logger.info(f"Completed paginated fetch for {object_key}. Total records retrieved: {len(all_records)}.")
    return all_records

def get_school_vespa_averages(school_id):
    """Calculate average VESPA scores for all students in a school."""
    if not school_id:
        app.logger.warning("get_school_vespa_averages called with no school_id.")
        return None

    app.logger.info(f"Calculating school VESPA averages for school_id: {school_id}")
    
    # Use the correct filter from tutor app.py - field_133 is the school connection
    filters_primary = [{'field': 'field_133', 'operator': 'is', 'value': school_id}]
    app.logger.info(f"Attempting to fetch all records for object_10 with primary filter: {filters_primary}")
    
    all_student_records_for_school = get_all_knack_records("object_10", filters=filters_primary)

    if not all_student_records_for_school:
        app.logger.warning(f"No student records found for school_id {school_id} using primary filter (field_133). Trying fallback filter (field_133_raw).")
        filters_fallback = [{'field': 'field_133_raw', 'operator': 'contains', 'value': school_id}]
        app.logger.info(f"Attempting to fetch all records for object_10 with fallback filter: {filters_fallback}")
        all_student_records_for_school = get_all_knack_records("object_10", filters=filters_fallback)
        
        if not all_student_records_for_school:
            app.logger.error(f"Could not retrieve any student records for school_id: {school_id} using primary or fallback filters. Cannot calculate averages.")
            return None
        app.logger.info(f"Retrieved {len(all_student_records_for_school)} student records for school_id {school_id} using fallback filter (field_133_raw).")
    else:
        app.logger.info(f"Retrieved {len(all_student_records_for_school)} student records for school_id {school_id} using primary filter (field_133).")
    
    vespa_elements = {
        "Vision": "field_147", "Effort": "field_148",
        "Systems": "field_149", "Practice": "field_150",
        "Attitude": "field_151", "Overall": "field_152",
    }
    sums = {key: 0 for key in vespa_elements}
    counts = {key: 0 for key in vespa_elements}

    # Now all_student_records_for_school is a flat list of student record dictionaries
    for record in all_student_records_for_school:
        # Ensure record is a dictionary before trying to .get() from it
        if not isinstance(record, dict):
            app.logger.warning(f"Skipping an item in all_student_records_for_school because it is not a dictionary: {type(record)} - Content: {str(record)[:100]}...")
            continue
            
        for element_name, field_key in vespa_elements.items():
            score_value = record.get(field_key)
            if score_value is not None:
                try:
                    score = float(score_value)
                    sums[element_name] += score
                    counts[element_name] += 1
                except (ValueError, TypeError):
                    app.logger.debug(f"Could not convert score '{score_value}' for {element_name} in record {record.get('id', 'N/A')} to float.")
    
    averages = {}
    for element_name in vespa_elements:
        if counts[element_name] > 0:
            averages[element_name] = round(sums[element_name] / counts[element_name], 2)
        else:
            averages[element_name] = 0
    
    app.logger.info(f"Calculated school VESPA averages for school_id {school_id}: {averages}")
    return averages

def normalize_qualification_type(exam_type_str):
    """Normalize qualification type strings to standard format."""
    if not exam_type_str:
        return "Unknown"
    
    exam_type_str = str(exam_type_str).strip()
    
    # A-Level variations
    if any(x in exam_type_str.upper() for x in ['A LEVEL', 'A-LEVEL', 'A2', 'ALEVEL']):
        return "A Level"
    
    # AS Level
    if 'AS LEVEL' in exam_type_str.upper() or 'AS-LEVEL' in exam_type_str.upper():
        return "AS Level"
    
    # IB
    if 'IB HL' in exam_type_str.upper() or 'INTERNATIONAL BACCALAUREATE HL' in exam_type_str.upper():
        return "IB HL"
    if 'IB SL' in exam_type_str.upper() or 'INTERNATIONAL BACCALAUREATE SL' in exam_type_str.upper():
        return "IB SL"
    
    # BTEC
    if 'BTEC' in exam_type_str.upper():
        if 'EXTENDED DIPLOMA' in exam_type_str.upper():
            return "BTEC Level 3 Extended Diploma"
        elif 'DIPLOMA' in exam_type_str.upper() and 'EXTENDED' not in exam_type_str.upper():
            return "BTEC Level 3 Diploma"
        elif 'SUBSIDIARY' in exam_type_str.upper():
            return "BTEC Level 3 Subsidiary Diploma"
        elif 'CERTIFICATE' in exam_type_str.upper():
            return "BTEC Level 3 Extended Certificate"
        else:
            return "BTEC Level 3"
    
    # Pre-U
    if 'PRE-U' in exam_type_str.upper() or 'PRE U' in exam_type_str.upper():
        if 'SHORT' in exam_type_str.upper():
            return "Pre-U Short Course"
        else:
            return "Pre-U Principal Subject"
    
    # UAL
    if 'UAL' in exam_type_str.upper():
        if 'EXTENDED' in exam_type_str.upper():
            return "UAL Level 3 Extended Diploma"
        elif 'DIPLOMA' in exam_type_str.upper():
            return "UAL Level 3 Diploma"
        else:
            return "UAL Level 3"
    
    # CACHE
    if 'CACHE' in exam_type_str.upper():
        if 'EXTENDED' in exam_type_str.upper():
            return "CACHE Level 3 Extended Diploma"
        elif 'DIPLOMA' in exam_type_str.upper():
            return "CACHE Level 3 Diploma"
        elif 'CERTIFICATE' in exam_type_str.upper():
            return "CACHE Level 3 Certificate"
        elif 'AWARD' in exam_type_str.upper():
            return "CACHE Level 3 Award"
        else:
            return "CACHE Level 3"
    
    return exam_type_str  # Return original if no match

def get_points(grade, qualification_type):
    """Convert grade to UCAS points based on qualification type."""
    if not grade or grade == "N/A": # Removed check for grade_points_mapping_kb here, will check inside
        app.logger.warning(f"get_points: Invalid input - grade: {grade}, qual_type: {qualification_type}")
        return 0
    
    grade_cleaned = str(grade).strip().upper()
    normalized_qual = normalize_qualification_type(qualification_type)
    # app.logger.debug(f"get_points: Looking for grade '{grade_cleaned}' in qual '{normalized_qual}'")

    if not grade_points_mapping_kb:
        app.logger.error("get_points: grade_points_mapping_kb is not loaded.")
        return 0

    qual_specific_map = grade_points_mapping_kb.get(normalized_qual)
    
    if not qual_specific_map:
        # app.logger.warning(f"get_points: No grade point mapping found for qualification type: '{normalized_qual}'. Attempting A-Level fallback if applicable.")
        if normalized_qual == "A Level": # A-Level specific fallback if not in main map
            grade_to_points_fallback = {
                'A*': 56, 'A': 48, 'B': 40, 'C': 32, 'D': 24, 'E': 16, 'U': 0
            }
            points = grade_to_points_fallback.get(grade_cleaned)
            if points is not None:
                # app.logger.info(f"get_points: Used A-Level fallback for grade '{grade_cleaned}', points: {points}")
                return points
            else:
                # app.logger.warning(f"get_points: A-Level fallback also failed for grade '{grade_cleaned}'. Available fallback grades: {list(grade_to_points_fallback.keys())}")
                pass # Fall through to return 0 at the end
        # app.logger.warning(f"get_points: No mapping for '{normalized_qual}' and not an A-Level fallback case. Returning 0.")
        return 0

    points = qual_specific_map.get(grade_cleaned)
    
    # Handle common variations like "Dist*" vs "D*"
    if points is None:
        if grade_cleaned == "DIST*": points = qual_specific_map.get("D*")
        elif grade_cleaned == "DIST": points = qual_specific_map.get("D")
        elif grade_cleaned == "MERIT": points = qual_specific_map.get("M")
        elif grade_cleaned == "PASS": points = qual_specific_map.get("P")
        # Add other BTEC/vocational grade variations if necessary, e.g. D*D*, DD, MM etc.
        # For example, if grade_cleaned is "D*D*" and qual_specific_map has "D*D*", it will be found.

    if points is not None:
        # app.logger.debug(f"get_points: Found points: {points} for grade '{grade_cleaned}' in '{normalized_qual}'.")
        return int(points)
    else:
        # app.logger.warning(f"get_points: No points found for grade '{grade_cleaned}' (original: '{grade}') in qualification '{normalized_qual}'. Available grades in map: {list(qual_specific_map.keys()) if qual_specific_map else 'N/A'}. Returning 0 points.")
        pass # Added pass to satisfy indentation for the else block
    return 0

def get_meg_for_prior_attainment(prior_attainment_score, qualification_type, percentile=75):
    """Get MEG based on prior attainment score and qualification type."""
    if prior_attainment_score is None:
        app.logger.warning(f"get_meg_for_prior_attainment: prior_attainment_score is None for qual '{qualification_type}'.")
        return "N/A", 0

    try:
        score = float(prior_attainment_score)
    except (ValueError, TypeError):
        app.logger.warning(f"get_meg_for_prior_attainment: Could not convert prior_attainment_score '{prior_attainment_score}' to float.")
        return "N/A", 0
    
    normalized_qual = normalize_qualification_type(qualification_type)
    
    benchmark_table_data = None
    if normalized_qual == "A Level":
        if percentile == 60:
            benchmark_table_data = alps_bands_aLevel_60_kb
        elif percentile == 75:
            benchmark_table_data = alps_bands_aLevel_75_kb
        elif percentile == 90:
            benchmark_table_data = alps_bands_aLevel_90_kb
        elif percentile == 100:
            benchmark_table_data = alps_bands_aLevel_100_kb
        else:
            app.logger.warning(f"get_meg_for_prior_attainment: Unsupported percentile '{percentile}' for A-Level. Defaulting to 75th.")
            benchmark_table_data = alps_bands_aLevel_75_kb
    # Add logic for other qualification types here if they have specific percentile tables
    # For now, if not A-Level, benchmark_table_data remains None and will hit the next check.

    if not benchmark_table_data:
        app.logger.warning(f"get_meg_for_prior_attainment: No ALPS benchmark table data loaded or selected for qual '{normalized_qual}', percentile '{percentile}'.")
        # Consider a more generic fallback or error handling if needed for non-A-Level quals
        # For now, this will lead to returning "N/A", 0 if no table is found.
        # If other qual types (BTEC, IB etc.) should use a default table (e.g. alps_bands_aLevel_75_kb as a proxy)
        # that logic would go here. For this fix, we focus on A-Level path.
        # Example: if normalized_qual == "AS Level": benchmark_table_data = alps_bands_aLevel_75_kb # Using 75th as proxy
        # This function's scope is currently limited by the KBs loaded for the student app.
        # The tutor app has more comprehensive ALPS band loading and selection.
        # For now, if not A-Level or table is missing, it will proceed to the loop (which won't run if benchmark_table_data is None)
        # and then hit the final fallback.
        if normalized_qual != "A Level":
             app.logger.info(f"get_meg_for_prior_attainment: No specific ALPS percentile table logic for '{normalized_qual}'. Will use general fallback if score not in bands.")
             # Attempt to use a default like A-Level 75th for non-A-Levels if a generic lookup is desired.
             # This depends on the expectation for non-A-Level MEGs.
             # For now, it will pass through and use the final fallback if the loop doesn't match.

    if benchmark_table_data: # Only proceed if a table was selected/loaded
        for band_info in benchmark_table_data: # benchmark_table_data is a list of dicts
            min_score_val = None
            max_score_val = None
            # More robust key checking, similar to tutor app
            possible_min_keys = ["gcseMinScore", "gcseMin", "Avg GCSE score Min", "Prior Attainment Min", "lowerBound"]
            possible_max_keys = ["gcseMaxScore", "gcseMax", "Avg GCSE score Max", "Prior Attainment Max", "upperBound"]
            possible_meg_keys = ["megAspiration", "MEG Aspiration", "minimumGrade", "megGrade", "MEG"]

            for key in possible_min_keys:
                if key in band_info:
                    min_score_val = band_info[key]
                    break
            for key in possible_max_keys:
                if key in band_info:
                    max_score_val = band_info[key]
                    break
            
            meg_aspiration_grade = "N/A"
            for key in possible_meg_keys:
                if key in band_info:
                    meg_aspiration_grade = band_info[key]
                    break
            
            if min_score_val is not None:
                try:
                    min_s = float(min_score_val)
                    # Determine max_s for range check. Assume inclusive max if only min_s is defined (point score)
                    # or exclusive max if max_score_val is present (range).
                    # This needs to align with how ALPS defines their bands.
                    # Common ALPS tables are [min_score, max_score) - min inclusive, max exclusive
                    max_s_is_exclusive_upper_bound = max_score_val is not None
                    max_s = float(max_score_val) if max_score_val is not None else float('inf')
                    
                    in_band = False
                    if max_s_is_exclusive_upper_bound:
                        if score >= min_s and score < max_s:
                            in_band = True
                    else: # If max_score_val is not typical, could be point-based or inclusive upper
                        if score >= min_s and (max_score_val is None or score <= max_s): # Default to inclusive if max_s logic unsure
                             in_band = True

                    if in_band:
                        meg_points_val = get_points(meg_aspiration_grade, normalized_qual)
                        # app.logger.info(f"get_meg_for_prior_attainment: Found band for score {score}. MEG Grade: {meg_aspiration_grade}, Points: {meg_points_val}")
                        return meg_aspiration_grade, meg_points_val if meg_points_val is not None else 0
                except (ValueError, TypeError) as e_conv:
                    app.logger.warning(f"get_meg_for_prior_attainment: Error converting band scores for band {band_info}: {e_conv}")
                    continue
        app.logger.warning(f"get_meg_for_prior_attainment: Score {score} not in any band of the selected table for qual '{normalized_qual}', percentile '{percentile}'. Table (first 200 chars): {str(benchmark_table_data)[:200]}...")
    else: # If benchmark_table_data was None (e.g. missing KB or non-Alevel without specific table)
        app.logger.warning(f"get_meg_for_prior_attainment: No benchmark_table_data to process for qual '{normalized_qual}', percentile '{percentile}'.")

    # Fallback if score not in any band or no table was processed
    default_grade_fallback = "N/A" 
    default_points_fallback = 0 # get_points for "N/A" should yield 0 with the updated get_points
    app.logger.warning(f"get_meg_for_prior_attainment: Using fallback MEG '{default_grade_fallback}' ({default_points_fallback} pts) for PA {score}, Qual '{normalized_qual}', Pctl '{percentile}'.")
    return default_grade_fallback, default_points_fallback

# Load additional ALPS KBs if available
alps_bands_aLevel_60_kb = load_json_file('alpsBands_aLevel_60.json')
alps_bands_aLevel_90_kb = load_json_file('alpsBands_aLevel_90.json')
alps_bands_aLevel_100_kb = load_json_file('alpsBands_aLevel_100.json')

# --- LLM Integration for Student Insights (adapted from tutorapp.py) ---
def generate_student_insights_with_llm(student_data_dict, app_logger_instance):
    """Generate personalized insights for students using OpenAI, adapted for student-facing content."""
    if not OPENAI_API_KEY:
        app.logger.warning("OpenAI API key not set. Returning placeholder insights.")
        # Return a structure consistent with what the frontend might expect, but with error messages
        return {
            "student_overview_summary": f"AI insights for {student_data_dict.get('student_name', 'you')} are currently unavailable (AI not configured).",
            "chart_comparative_insights": "Insights unavailable (AI not configured).",
            "questionnaire_interpretation_and_reflection_summary": "Questionnaire interpretation unavailable (AI not configured).",
            "academic_benchmark_analysis": "Academic benchmark analysis unavailable (AI not configured).",
            "suggested_student_goals": ["Goal suggestions unavailable (AI not configured)."]
        }

    try:
        openai.api_key = OPENAI_API_KEY # Ensure openai object is used if you aliased it, e.g. client.api_key
        app_logger_instance.info(f"Attempting to generate LLM insights for student: {student_data_dict.get('student_name', 'N/A')}")

        student_name = student_data_dict.get('student_name', 'Student')
        student_level = student_data_dict.get('student_level', 'N/A') 
        current_cycle = student_data_dict.get('current_cycle', 'N/A')
        school_averages = student_data_dict.get('school_vespa_averages') 
        vespa_profile_for_rag = student_data_dict.get('vespa_profile', {}) 
        all_scored_questionnaire_statements = student_data_dict.get('all_scored_questionnaire_statements', [])

        prompt_parts = []
        prompt_parts.append(f"You are My VESPA AI Coach. I am '{student_name}'. This is my data:")
        prompt_parts.append(f"Current Cycle: {current_cycle}.")

        prompt_parts.append("\n--- My Current VESPA Profile (Vision, Effort, Systems, Practice, Attitude) ---")
        if student_data_dict.get('vespa_profile'):
            for element, details in student_data_dict['vespa_profile'].items():
                if element == "Overall": continue
                prompt_parts.append(f"- {element}: My score is {details.get('score_1_to_10', 'N/A')}/10, which is considered '{details.get('score_profile_text', 'N/A')}'.")

        if school_averages:
            prompt_parts.append("\n--- School's Average VESPA Scores (For Comparison) ---")
            for element, avg_score in school_averages.items():
                prompt_parts.append(f"- {element} (School Avg): {avg_score}/10")
        
        prompt_parts.append("\n--- My Academic Profile (First 3 Subjects with My Standard Expected Grade) ---")
        if student_data_dict.get('academic_profile_summary'):
            profile_data = student_data_dict['academic_profile_summary']
            valid_subjects_shown = 0
            if isinstance(profile_data, list) and profile_data and \
               not (profile_data[0].get('subject','').startswith("Academic profile not found")) and \
               not (profile_data[0].get('subject','').startswith("No academic subjects parsed")):
                for subject_info in profile_data[:3]:
                    if subject_info.get('subject') and subject_info.get('subject') != "N/A":
                        meg_text = f", My Standard Expected Grade (MEG): {subject_info.get('standard_meg', 'N/A')}" if subject_info.get('standard_meg') else ""
                        prompt_parts.append(f"- Subject: {subject_info.get('subject')}, My Current Grade: {subject_info.get('currentGrade', 'N/A')}, My Target: {subject_info.get('targetGrade', 'N/A')}{meg_text}")
                        valid_subjects_shown += 1
                if valid_subjects_shown == 0:
                    prompt_parts.append("  It looks like my detailed subject information isn't available right now.")        
            else:
                prompt_parts.append("  My detailed academic profile summary isn't available at the moment.")
        
        if student_data_dict.get('academic_megs'):
            meg_data = student_data_dict['academic_megs']
            prompt_parts.append("\n--- My Academic Benchmarks (Based on My Prior Attainment) ---")
            prompt_parts.append(f"  My GCSE Prior Attainment Score: {meg_data.get('prior_attainment_score', 'N/A')}")
            if meg_data.get('aLevel_meg_grade_75th') and meg_data.get('aLevel_meg_grade_75th') != "N/A":
                 prompt_parts.append(f"  For A-Levels, students with similar prior scores typically achieve around a grade '{meg_data.get('aLevel_meg_grade_75th')}' (this is the standard MEG or top 25% benchmark).")

        prompt_parts.append("\n--- My Reflections & Goals (Current Cycle) ---")
        reflections_goals_found_student = False
        current_rrc_text_student = "Not specified"
        current_goal_text_student = "Not specified"
        if student_data_dict.get('student_reflections_and_goals'):
            reflections = student_data_dict['student_reflections_and_goals']
            current_rrc_key_student = f"rrc{current_cycle}_comment"
            current_goal_key_student = f"goal{current_cycle}"
            rrc_val = reflections.get(current_rrc_key_student)
            goal_val = reflections.get(current_goal_key_student)

            if rrc_val and rrc_val != "Not specified":
                current_rrc_text_student = str(rrc_val)[:300].replace('\n', ' ')
                prompt_parts.append(f"- My Current Reflection (RRC{current_cycle}): {current_rrc_text_student}...")
                reflections_goals_found_student = True
            if goal_val and goal_val != "Not specified":
                current_goal_text_student = str(goal_val)[:300].replace('\n', ' ')
                prompt_parts.append(f"- My Current Goal (Goal {current_cycle}): {current_goal_text_student}...")
                reflections_goals_found_student = True
        if not reflections_goals_found_student:
            prompt_parts.append("  I haven't specified any reflections or goals for the current cycle, or they are not available.")

        prompt_parts.append("\n--- My Key Questionnaire Insights (My Top & Bottom Scoring Statements) ---")
        obj29_highlights = student_data_dict.get("object29_question_highlights")
        if obj29_highlights:
            if obj29_highlights.get("top_3") and obj29_highlights["top_3"]:
                prompt_parts.append("  Statements I Most Agreed With (1-5 scale, 5=Strongly Agree):")
                for q_data in obj29_highlights["top_3"]:
                    prompt_parts.append(f"    - Score {q_data.get('score', 'N/A')}/5 ({q_data.get('category', 'N/A')}): \"{q_data.get('text', 'N/A')}\"")
            if obj29_highlights.get("bottom_3") and obj29_highlights["bottom_3"]:
                prompt_parts.append("  Statements I Least Agreed With (Areas to think about):")
                for q_data in obj29_highlights["bottom_3"]:
                    prompt_parts.append(f"    - Score {q_data['score']}/5 ({q_data['category']}): \"{q_data['text']}\"")
        else:
            prompt_parts.append("  My top/bottom questionnaire statement highlights are not available.")

        # Overall Questionnaire Statement Response Distribution (Student view)
        prompt_parts.append("\n--- My Overall Questionnaire Statement Response Distribution ---")
        if all_scored_questionnaire_statements and isinstance(all_scored_questionnaire_statements, list):
            response_counts = {1: 0, 2: 0, 3: 0, 4: 0, 5: 0}
            for q_data in all_scored_questionnaire_statements:
                score = q_data.get("score")
                if score in response_counts: response_counts[score] += 1
            prompt_parts.append(f"  - Statements I rated '1' (e.g., Strongly Disagree): {response_counts[1]}")
            prompt_parts.append(f"  - Statements I rated '2': {response_counts[2]}")
            prompt_parts.append(f"  - Statements I rated '3': {response_counts[3]}")
            prompt_parts.append(f"  - Statements I rated '4': {response_counts[4]}")
            prompt_parts.append(f"  - Statements I rated '5' (e.g., Strongly Agree): {response_counts[5]}")
        else:
            prompt_parts.append("  My detailed questionnaire response distribution data is not available.")

        # --- TASKS FOR THE AI (Student View) ---
        prompt_parts.append("\n\n--- Coach, please help me with these things: ---")
        prompt_parts.append("Based ONLY on my data provided above, please provide the following insights FOR ME ('{student_name}').")
        prompt_parts.append("Your tone should be encouraging, supportive, and help me understand myself better. Give me practical, actionable advice. Subtly draw upon general coaching principles and insights related to mindset, goal-setting, self-reflection, and VESPA elements when formulating your responses, especially for the questionnaire analysis and overview. Frame suggestions as reflective points for me.")
        prompt_parts.append("Please format your entire response as a single JSON object with the following EXACT keys: \"student_overview_summary\", \"chart_comparative_insights\", \"questionnaire_interpretation_and_reflection_summary\", \"academic_benchmark_analysis\", \"suggested_student_goals\", \"academic_quote\", \"academic_performance_ai_summary\".")
        prompt_parts.append("Ensure all string values within the JSON are properly escaped.")
        
        # --- RAG Elements for student prompt (Simplified for now, can be expanded) ---
        # This section is less about the tutor's KB and more about general advice based on lowest VESPA or similar
        lowest_vespa_element_student = None
        lowest_score_student = 11 
        if vespa_profile_for_rag:
            for element, details in vespa_profile_for_rag.items():
                if element == "Overall": continue
                try:
                    score = float(details.get('score_1_to_10', 10))
                    if score < lowest_score_student:
                        lowest_score_student = score
                        lowest_vespa_element_student = element
                except (ValueError, TypeError): pass

        if lowest_vespa_element_student:
            prompt_parts.append("\n\n--- Some Ideas to Consider ---")
            prompt_parts.append(f"My lowest VESPA score seems to be in '{lowest_vespa_element_student}'. Can you give me some general tips or reflective questions for this area, and perhaps suggest a simple, actionable goal related to it? You can use the general reflective statements and coaching insights from your knowledge base for inspiration.")
            # We don't directly inject KB content into student prompt like we do for tutor, 
            # but we ask the LLM to use its general knowledge inspired by such KBs.

        # --- Knowledge Base Excerpts (Student View - less direct, more for LLM's internal inspiration) ---
        # We won't show the student the raw KB excerpts like we did for the tutor.
        # Instead, the prompt will guide the LLM to use this type of knowledge implicitly.
        # Example: If coaching_kb and REFLECTIVE_STATEMENTS_DATA are globally available in this backend scope:
        relevant_coaching_insights = []
        if COACHING_INSIGHTS_DATA and isinstance(COACHING_INSIGHTS_DATA, list):
            # Attempt to find a few relevant insights based on keywords or student's lowest VESPA.
            # This is a simple keyword match; more advanced RAG could be used.
            keywords_from_student_data = [student_name.lower(), lowest_vespa_element_student.lower() if lowest_vespa_element_student else ""]
            if student_data_dict.get('student_reflections_and_goals'):
                rrc_text_for_kw = student_data_dict['student_reflections_and_goals'].get(f"rrc{current_cycle}_comment", "").lower()
                goal_text_for_kw = student_data_dict['student_reflections_and_goals'].get(f"goal{current_cycle}", "").lower()
                keywords_from_student_data.extend(rrc_text_for_kw.split()[:10]) # First 10 words
                keywords_from_student_data.extend(goal_text_for_kw.split()[:10])

            # Add keywords from top/bottom questionnaire statements if available
            if obj29_highlights:
                for q_data in obj29_highlights.get("top_3", []) + obj29_highlights.get("bottom_3", []):
                    keywords_from_student_data.extend(q_data.get('text', '').lower().split()[:5])


            # Filter out very common words to make keywords more meaningful for matching insights
            common_filter_words = {"i", "me", "my", "is", "a", "the", "and", "to", "of", "it", "in", "for", "on", "with", "as", "an", "at", "by", "you", "your", "what", "how", "help", "can", "some", "this", "that", "area", "areas", "score", "scores"}
            meaningful_keywords = [kw for kw in keywords_from_student_data if kw not in common_filter_words and len(kw) > 3]


            for insight in COACHING_INSIGHTS_DATA:
                insight_text_corpus = (
                    str(insight.get('name', '')).lower() + " " +
                    str(insight.get('description', '')).lower() + " " +
                    str(insight.get('implications_for_tutor', '')).lower() + " " +
                    " ".join(insight.get('keywords', [])).lower()
                )
                # Check if any of the student's meaningful keywords appear in the insight's text corpus
                if any(m_kw in insight_text_corpus for m_kw in meaningful_keywords):
                    if len(relevant_coaching_insights) < 3: # Limit to 3 for brevity in prompt
                        insight_summary_for_prompt = f"Insight: {insight.get('name')}. Focus: {insight.get('description')[:100]}..."
                        relevant_coaching_insights.append(insight_summary_for_prompt)
            
        if relevant_coaching_insights:
            prompt_parts.append("\n\n--- General Coaching Principles (For AI's Inspiration) ---")
            prompt_parts.append("Remember to draw inspiration from general coaching principles. For example, here are a few themes from your knowledge base that might be relevant to consider when interpreting my data and suggesting reflections (do not quote these directly, but use the underlying ideas):")
            for RAG_insight_summary in relevant_coaching_insights:
                prompt_parts.append(f"- {RAG_insight_summary}")
        
        if coaching_kb: # This KB is 'coaching_questions_knowledge_base.json'
            prompt_parts.append("\n(For the AI: You also have access to a coaching questions knowledge base. Use its principles to help formulate your advice and goal suggestions, aiming for reflective and empowering questions for me, '{student_name}'.)")
        if REFLECTIVE_STATEMENTS_DATA:
            prompt_parts.append("(For the AI: You also have access to a list of general reflective statements. These can inspire the tone and nature of the S.M.A.R.T. goals you suggest for me.)")


        # --- REQUIRED OUTPUT STRUCTURE (JSON Object - Student View) ---
        prompt_parts.append("\n\n--- REQUIRED OUTPUT STRUCTURE (JSON Object) ---")
        prompt_parts.append("Please provide your response as a single, valid JSON object. Example:")
        prompt_parts.append("'''") # Start of code block marker for prompt
        prompt_parts.append("{")
        prompt_parts.append("  \"student_overview_summary\": \"A concise 2-3 sentence AI Student Snapshot for me, '{student_name}', highlighting 1-2 of my key strengths and 1-2 primary areas for development, rooted in VESPA principles and drawing from general coaching themes. Max 100-120 words. Speak directly to me (e.g., 'Your data shows...', 'You could focus on...').\",")
        prompt_parts.append("  \"chart_comparative_insights\": \"A short paragraph (max 100 words) helping me understand my VESPA scores compared to the school averages (if provided). What could these differences or similarities mean for me? If a score is significantly different, suggest a brief reflective question for me based on general coaching principles related to that VESPA element (e.g., if 'Systems' is low, 'What's one small organizational change you could try?'). Use 'you' and 'your'.\",")
        prompt_parts.append("  \"questionnaire_interpretation_and_reflection_summary\": \"A concise summary (approx. 150-200 words) interpreting my overall questionnaire responses (e.g., my tendencies towards 'Strongly Disagree' or 'Strongly Agree', as indicated by the counts of 1s, 2s, etc.). Highlight any notable patterns, such as a concentration of low or high responses in specific VESPA elements (refer to my Top/Bottom scoring statements). Subtly connect these patterns to general coaching insights about mindset, self-reflection, or goal-setting (e.g., if responses suggest a fixed mindset, gently introduce the idea of growth without being preachy). Also, briefly compare and contrast these questionnaire insights with my own RRC/Goal comments (My RRC: '{RRC_COMMENT_PLACEHOLDER}', My Goal: '{GOAL_COMMENT_PLACEHOLDER}'), noting any consistencies or discrepancies that could be valuable for me to reflect on. Use 'you' and 'your'.\",")
        prompt_parts.append("  \"academic_benchmark_analysis\": \"A supportive and encouraging analysis (approx. 150-180 words) of my academic performance. Start by looking at my current grades in relation to my Subject Target Grades and my Standard Expected Grades (MEGs). Explain that MEGs show what students with similar prior GCSE scores typically achieve (top 25%) and are aspirational. Explain that my Subject Target Grade (STG) is a more nuanced target that considers subject difficulty. Emphasize that comparing my current grades, MEGs, and STGs should help me think about my progress, strengths, and potential next steps. The goal is to use this information to identify areas for support or challenge, always considering my broader context. Use 'you' and 'your'.\",")
        prompt_parts.append("  \"suggested_student_goals\": [\"Based on the analysis, and inspired by general reflective statements and coaching principles (e.g., focusing on an area for development from the questionnaire or VESPA profile), suggest 2-3 S.M.A.R.T. goals FOR ME, reframed to my context. Make them actionable and specific.\", \"Goal 2...\"],")
        prompt_parts.append("  \"academic_quote\": \"A short, inspirational or funny quote suitable for a student. e.g., 'The expert in anything was once a beginner.' or 'Why fall in love when you can fall asleep?'\",")
        prompt_parts.append("  \"academic_performance_ai_summary\": \"A kind, encouraging, and professional AI summary (like a helpful teacher, approx. 200-250 words) analyzing my academic profile. Discuss my subject benchmarks in relation to my MEGs. If I'm not meeting MEGs, be gentle and positive, focusing on growth and understanding. Highlight strengths and areas for development based on my subject performance. The tone should be positive and empowering, even when pointing out challenges. Reference the MEG explainer text that I will see, which describes MEGs as aspirational and STGs as more personalized. Use 'you' and 'your'.\"")
        prompt_parts.append("}")
        prompt_parts.append("'''") # End of code block marker

        # Prepare cleaned versions of current_rrc_text and current_goal_text for the prompt placeholder replacement
        cleaned_rrc_placeholder_student = current_rrc_text_student[:100].replace('\n', ' ').replace("'", "\\'").replace('"', '\\"')
        cleaned_goal_placeholder_student = current_goal_text_student[:100].replace('\n', ' ').replace("'", "\\'").replace('"', '\\"')
        prompt_parts.append(f"REMEMBER to replace RRC_COMMENT_PLACEHOLDER with: '{cleaned_rrc_placeholder_student}...' and GOAL_COMMENT_PLACEHOLDER with: '{cleaned_goal_placeholder_student}...' in your actual questionnaire_interpretation_and_reflection_summary output.")

        prompt_to_send = "\n".join(prompt_parts)
        # Substitute placeholders in the final prompt string
        prompt_to_send = prompt_to_send.replace("'{RRC_COMMENT_PLACEHOLDER}'", f"'{cleaned_rrc_placeholder_student}...'")
        prompt_to_send = prompt_to_send.replace("'{GOAL_COMMENT_PLACEHOLDER}'", f"'{cleaned_goal_placeholder_student}...'")

        app_logger_instance.info(f"Generated Student LLM Prompt (first 500 chars): {prompt_to_send[:500]}")
        app_logger_instance.info(f"Generated Student LLM Prompt (last 500 chars): {prompt_to_send[-500:]}")
        app_logger_instance.info(f"Total Student LLM Prompt length: {len(prompt_to_send)} characters")

        system_message_content = (
            f"You are My VESPA AI Coach, an AI assistant designed to help students understand their VESPA profile (Vision, Effort, Systems, Practice, Attitude) "
            f"and academic performance. Your responses should be encouraging, supportive, and provide clear, actionable advice directly to the student using 'you' and 'your'. "
            f"You are speaking to '{student_name}'. Help them reflect on their data and identify steps for improvement. Your output MUST be a single JSON object with specific keys."
        )

        max_retries = 2
        for attempt in range(max_retries):
            try:
                # Using openai.chat.completions.create for newer OpenAI library versions
                response = openai.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[
                        {"role": "system", "content": system_message_content},
                        {"role": "user", "content": prompt_to_send}
                    ],
                    max_tokens=1200, # Adjusted for potentially detailed student-facing JSON, increased slightly
                    temperature=0.65, # Slightly higher for more nuanced and less wooden student advice
                    n=1,
                    stop=None,
                    response_format={"type": "json_object"} # Request JSON output
                )
                
                raw_response_content = response.choices[0].message.content.strip()
                app_logger_instance.info(f"Student LLM raw response: {raw_response_content}")

                parsed_llm_outputs = json.loads(raw_response_content)
                
                # Validate expected keys for student response
                expected_keys_student = [
                    "student_overview_summary", 
                    "chart_comparative_insights", 
                    "questionnaire_interpretation_and_reflection_summary", 
                    "academic_benchmark_analysis", 
                    "suggested_student_goals",
                    "academic_quote",
                    "academic_performance_ai_summary"
                ]
                # Fill missing keys with error messages if LLM doesn't provide them
                all_keys_present = True
                for key in expected_keys_student:
                    if key not in parsed_llm_outputs:
                        all_keys_present = False
                        parsed_llm_outputs[key] = f"Error: AI response for '{key}' was not provided."
                if not all_keys_present:
                    app_logger_instance.warning(f"Student LLM response missing one or more expected keys. Filled with defaults. Response: {raw_response_content}")
                
                app_logger_instance.info(f"Student LLM generated structured data: {parsed_llm_outputs}")
                return parsed_llm_outputs

            except json.JSONDecodeError as e_json:
                app_logger_instance.error(f"JSONDecodeError from Student LLM response (Attempt {attempt + 1}/{max_retries}): {e_json}")
                app_logger_instance.error(f"Problematic Student LLM response content: {raw_response_content}")
                if attempt == max_retries - 1:
                    return {key: f"Error parsing AI response for {key} after multiple attempts." for key in expected_keys_student}
            except Exception as e_general:
                app_logger_instance.error(f"Error calling OpenAI API or processing response for student (Attempt {attempt + 1}/{max_retries}): {e_general}")
                if attempt == max_retries - 1:
                     return {key: f"Error generating insights from AI for {key}. (Details: {str(e_general)[:50]}...)" for key in expected_keys_student}
            time.sleep(1) # Wait before retrying

        # Fallback if all retries fail
        app_logger_instance.error("Student LLM processing failed after all retries.")
        return {key: "Critical error: AI processing failed after all retries." for key in expected_keys_student}

    except Exception as e_outer:
        app_logger_instance.error(f"Outer exception in generate_student_insights_with_llm: {e_outer}")
        return {
            "student_overview_summary": "An unexpected error occurred while generating AI insights.",
            "chart_comparative_insights": "Insights unavailable due to an error.",
            "questionnaire_interpretation_and_reflection_summary": "Questionnaire interpretation unavailable due to an error.",
            "academic_benchmark_analysis": "Academic benchmark analysis unavailable due to an error.",
            "suggested_student_goals": ["Goal suggestions unavailable due to an error."],
            "academic_quote": "Quote unavailable due to an error.",
            "academic_performance_ai_summary": "Personalized academic summary unavailable due to an error."
        }

# --- Main API Endpoint --- 
@app.route('/api/v1/student_coaching_data', methods=['POST', 'OPTIONS'])
def student_coaching_data():
    app.logger.info(f"Received request for /api/v1/student_coaching_data. Method: {request.method}")
    if request.method == 'OPTIONS':
        return _build_cors_preflight_response()
    
    if request.method == 'POST':
        data = request.get_json()
        student_object3_id = data.get('student_object3_id')
        if not student_object3_id:
            app.logger.error("Missing 'student_object3_id' in request.")
            return jsonify({"error": "Missing student_object3_id"}), 400

        app.logger.info(f"Processing data for student Object_3 ID: {student_object3_id}")

        # 1. Fetch Student User Details (Object_3)
        student_user_data = get_student_user_details(student_object3_id)
        student_email = None
        student_name_from_obj3 = f"Student_{student_object3_id[:6]}" # Default
        if student_user_data:
            # field_70 in Object_3 is the email field.
            # Knack email fields when fetched as objects can be like: {'email': 'actual.email@example.com', 'label': 'actual.email@example.com'}
            # Or the _raw version might sometimes just be the string, or an HTML string as observed.
            raw_val_field70 = student_user_data.get('field_70_raw') # Knack raw value
            obj_val_field70 = student_user_data.get('field_70') # Knack object value

            # Priority 1: Try to get email from field_70 if it's a Knack email object
            if isinstance(obj_val_field70, dict) and 'email' in obj_val_field70 and isinstance(obj_val_field70['email'], str):
                student_email = obj_val_field70['email'].strip()
            # Priority 2: Try to get email from field_70_raw if it's a Knack email object (less common for _raw to be object)
            elif isinstance(raw_val_field70, dict) and 'email' in raw_val_field70 and isinstance(raw_val_field70['email'], str):
                 student_email = raw_val_field70['email'].strip()
            # Priority 3: If field_70_raw is a string (as suggested by logs)
            elif isinstance(raw_val_field70, str):
                temp_email_str = raw_val_field70.strip()
                # Check if it's an HTML string like <a href="mailto:email@example.com">text</a>
                if temp_email_str.lower().startswith('<a') and 'mailto:' in temp_email_str.lower() and temp_email_str.lower().endswith('</a>'):
                    try:
                        # Extract from within mailto:"..." part of href
                        mailto_keyword = 'mailto:'
                        mailto_start_index = temp_email_str.lower().find(mailto_keyword) + len(mailto_keyword)
                        
                        # Find the end of the email address in href. It could be terminated by ", ', >.
                        end_char_index = len(temp_email_str) 
                        
                        quote_index = temp_email_str.find('"', mailto_start_index)
                        if quote_index != -1:
                            end_char_index = min(end_char_index, quote_index)
                        
                        single_quote_index = temp_email_str.find("'", mailto_start_index)
                        if single_quote_index != -1:
                            end_char_index = min(end_char_index, single_quote_index)
                            
                        angle_bracket_index = temp_email_str.find('>', mailto_start_index)
                        if angle_bracket_index != -1: # Should be present if parsing href part of <a> tag
                            end_char_index = min(end_char_index, angle_bracket_index)

                        extracted_from_href = temp_email_str[mailto_start_index:end_char_index].strip()
                        
                        if '@' in extracted_from_href and ' ' not in extracted_from_href and '<' not in extracted_from_href:
                            student_email = extracted_from_href
                        
                        # Fallback: if mailto parsing didn't yield a good email, try getting link text
                        if not student_email:
                            text_start_actual_index = temp_email_str.find('>') 
                            if text_start_actual_index != -1:
                                text_start_actual_index +=1 # move past '>'
                                text_end_index = temp_email_str.lower().rfind('</a>') # Use rfind for last occurrence
                                if text_end_index > text_start_actual_index :
                                    extracted_text = temp_email_str[text_start_actual_index:text_end_index].strip()
                                    if '@' in extracted_text and ' ' not in extracted_text and '<' not in extracted_text:
                                        student_email = extracted_text
                                        app.logger.info(f"Used email from link text: {student_email}")

                    except Exception as e_parse:
                        app.logger.warning(f"Error parsing specific HTML email string '{temp_email_str}': {e_parse}")
                # If it's just a plain email string (no HTML detected or parsing failed)
                elif '@' in temp_email_str and not '<' in temp_email_str:
                    student_email = temp_email_str
            # Priority 4: Fallback to field_70 if it's a plain string and others failed
            elif isinstance(obj_val_field70, str) and '@' in obj_val_field70 and not '<' in obj_val_field70 :
                 student_email = obj_val_field70.strip()
            
            if student_email:
                app.logger.info(f"Extracted student email: {student_email} for Object_3 ID {student_object3_id}")
            else:
                app.logger.warning(f"Could not extract plain email from Object_3 field_70 for ID {student_object3_id}. "
                                   f"field_70_raw: '{raw_val_field70}', field_70: '{obj_val_field70}'")

            # Correctly get student name from field_69 (Name field in Object_3)
            name_data_raw = student_user_data.get('field_69_raw')
            name_data_obj = student_user_data.get('field_69')

            if isinstance(name_data_raw, dict) and name_data_raw.get('full'):
                student_name_from_obj3 = name_data_raw['full']
            elif isinstance(name_data_obj, dict) and name_data_obj.get('full'):
                student_name_from_obj3 = name_data_obj['full']
            elif isinstance(name_data_raw, dict): # Fallback if 'full' isn't present but it's a dict
                first = name_data_raw.get('first', '')
                last = name_data_raw.get('last', '')
                title = name_data_raw.get('title', '')
                constructed_name = f"{title} {first} {last}".replace('  ', ' ').strip()
                if constructed_name and constructed_name != title: # Ensure some name was actually built
                    student_name_from_obj3 = constructed_name
            elif isinstance(name_data_obj, dict): # Fallback for object version
                first = name_data_obj.get('first', '')
                last = name_data_obj.get('last', '')
                title = name_data_obj.get('title', '')
                constructed_name = f"{title} {first} {last}".replace('  ', ' ').strip()
                if constructed_name and constructed_name != title:
                    student_name_from_obj3 = constructed_name
            # If it's a direct string (less common for Knack name fields but possible)
            elif isinstance(name_data_raw, str) and name_data_raw.strip():
                student_name_from_obj3 = name_data_raw.strip()
            elif isinstance(name_data_obj, str) and name_data_obj.strip():
                student_name_from_obj3 = name_data_obj.strip()
        else:
            app.logger.warning(f"Could not fetch Object_3 details for ID {student_object3_id}")
            # Return error or limited dummy if core student info fails
            return jsonify({"error": f"Could not retrieve user details for {student_object3_id}"}), 404

        # 2. Fetch Student's VESPA Profile (Object_10)
        object10_data = get_student_object10_record(student_email) if student_email else None
        current_cycle = 0
        vespa_scores_for_profile = {}
        student_reflections = {}
        school_id = None
        school_vespa_averages = None
        
        if object10_data:
            current_cycle_str = object10_data.get("field_146_raw", "0")
            # Ensure current_cycle_str is treated as a string before isdigit()
            current_cycle = int(str(current_cycle_str)) if str(current_cycle_str).isdigit() else 0
            app.logger.info(f"Student's current cycle from Object_10: {current_cycle}")
            
            # Get school ID for averages calculation (from tutor app.py)
            school_id = None
            school_connection_raw = object10_data.get("field_133_raw")
            if isinstance(school_connection_raw, list) and school_connection_raw:
                school_id = school_connection_raw[0].get('id')
                app.logger.info(f"Extracted school_id '{school_id}' from student's Object_10 field_133_raw (list).")
            elif isinstance(school_connection_raw, str):
                school_id = school_connection_raw
                app.logger.info(f"Extracted school_id '{school_id}' (string) from student's Object_10 field_133_raw.")
            else:
                # Attempt to get from non-raw field if raw is not helpful
                school_connection_obj = object10_data.get("field_133")
                if isinstance(school_connection_obj, list) and school_connection_obj:
                    school_id = school_connection_obj[0].get('id')
                    app.logger.info(f"Extracted school_id '{school_id}' from student's Object_10 field_133 (non-raw object).")
                else:
                    app.logger.warning(f"Could not determine school_id from field_133_raw or field_133. Data (raw): {school_connection_raw}, Data (obj): {school_connection_obj}")
            
            vespa_scores_for_profile = {
                "Vision": {"score_1_to_10": object10_data.get("field_147"), "score_profile_text": get_score_profile_text(object10_data.get("field_147"))},
                "Effort": {"score_1_to_10": object10_data.get("field_148"), "score_profile_text": get_score_profile_text(object10_data.get("field_148"))},
                "Systems": {"score_1_to_10": object10_data.get("field_149"), "score_profile_text": get_score_profile_text(object10_data.get("field_149"))},
                "Practice": {"score_1_to_10": object10_data.get("field_150"), "score_profile_text": get_score_profile_text(object10_data.get("field_150"))},
                "Attitude": {"score_1_to_10": object10_data.get("field_151"), "score_profile_text": get_score_profile_text(object10_data.get("field_151"))}
            }
            student_reflections = {
                f"rrc{current_cycle}_comment": object10_data.get(f"field_{2301+current_cycle}"), # RRC1=2302, RRC2=2303, RRC3=2304
                f"goal{current_cycle}": object10_data.get(f"field_{2498+current_cycle}" if current_cycle==1 else f"field_{2491+current_cycle}") # Goal1=2499, Goal2=2493, Goal3=2494
            }
            
            # Calculate school VESPA averages
            school_vespa_averages = None
            if school_id:
                school_vespa_averages = get_school_vespa_averages(school_id)
                if school_vespa_averages:
                    app.logger.info(f"Successfully retrieved school-wide VESPA averages for school {school_id}: {school_vespa_averages}")
                else:
                    app.logger.warning(f"Failed to retrieve school-wide VESPA averages for school {school_id}.")
            else:
                app.logger.warning("Cannot fetch school-wide VESPA averages as school_id is unknown.")
        else:
            app.logger.warning(f"No Object_10 data for student {student_name_from_obj3} (Email: {student_email})")
            # Populate with N/A or defaults if Object_10 is missing
            for v_element in ["Vision", "Effort", "Systems", "Practice", "Attitude"]:
                vespa_scores_for_profile[v_element] = {"score_1_to_10": "N/A", "score_profile_text": "N/A"}
            student_reflections = {"rrc_comment": "Not available", "goal": "Not available"}

        # 3. Fetch Questionnaire Data (Object_29)
        all_scored_statements = []
        object29_highlights_top_bottom = {"top_3": [], "bottom_3": []}
        if object10_data and current_cycle > 0 and psychometric_question_details_kb:
            object29_data = get_student_object29_questionnaire_data(object10_data.get('id'), current_cycle)
            if object29_data:
                for q_detail in psychometric_question_details_kb:
                    field_id = q_detail.get('currentCycleFieldId') # These are generic like field_794
                    if not field_id: continue
                    raw_score = object29_data.get(field_id) # Or field_id + "_raw" depending on Knack field type
                    if raw_score is None and field_id.startswith("field_"):
                        score_obj = object29_data.get(field_id + '_raw')
                        if isinstance(score_obj, dict):
                            raw_score = score_obj.get('value') 
                        elif score_obj is not None:
                            raw_score = score_obj
                    
                    try:
                        score = int(raw_score)
                        all_scored_statements.append({
                            "text": q_detail.get('questionText', 'Unknown Question'), # Changed key to 'text'
                            "score": score,
                            "category": q_detail.get('vespaCategory', 'N/A')
                        })
                    except (ValueError, TypeError):
                        app.logger.debug(f"Could not parse score '{raw_score}' for {field_id} in Object_29.")
                
                if all_scored_statements:
                    all_scored_statements.sort(key=lambda x: x["score"])
                    object29_highlights_top_bottom["bottom_3"] = all_scored_statements[:3]
                    object29_highlights_top_bottom["top_3"] = all_scored_statements[-3:][::-1]
            else:
                 app.logger.warning(f"No Object_29 data retrieved for student {student_name_from_obj3}, cycle {current_cycle}")       
        elif not psychometric_question_details_kb:
            app.logger.error("Psychometric Question Details KB not loaded. Cannot process Object_29.")

        # 4. Fetch Academic Profile (Object_112)
        academic_summary = []
        academic_megs = {}
        prior_attainment_score = None
        object112_profile_record_data = None # To store the whole Object_112 record
        
        # Call the new get_academic_profile function
        # Pass app.logger as the app_logger_instance argument
        academic_profile_response = get_academic_profile(student_object3_id, student_name_from_obj3, app.logger)
        
        if academic_profile_response:
            academic_summary = academic_profile_response.get("subjects", [])
            object112_profile_record_data = academic_profile_response.get("profile_record")

        if object112_profile_record_data: # Check if the record itself was found
            app.logger.info(f"Fetched Object_112 data for student: {object112_profile_record_data.get('field_3066')} (Name in Obj112)")
            
            # Get prior attainment score (field_3272 from tutorapp.py)
            # Ensure robust checking for _raw and direct field, and convert to float
            prior_attainment_val = None
            raw_pa = object112_profile_record_data.get('field_3272_raw')
            direct_pa = object112_profile_record_data.get('field_3272')

            if isinstance(raw_pa, (str, int, float)) and str(raw_pa).strip() != '':
                try: prior_attainment_val = float(raw_pa)
                except (ValueError, TypeError): pass
            
            if prior_attainment_val is None and isinstance(direct_pa, (str, int, float)) and str(direct_pa).strip() != '':
                try: prior_attainment_val = float(direct_pa)
                except (ValueError, TypeError): pass

            if prior_attainment_val is not None:
                prior_attainment_score = prior_attainment_val
                app.logger.info(f"Prior attainment score: {prior_attainment_score}")
            else:
                app.logger.warning(f"Could not parse prior attainment score from field_3272/field_3272_raw in Object_112. Raw: '{raw_pa}', Direct: '{direct_pa}'.")
            
            # Calculate overall MEGs if prior attainment is available
            if prior_attainment_score is not None:
                academic_megs["prior_attainment_score"] = prior_attainment_score
                
                for percentile, label_suffix in [(60, "60th"), (75, "75th"), (90, "90th"), (100, "100th")]:
                    meg_grade, meg_points = get_meg_for_prior_attainment(prior_attainment_score, "A Level", percentile)
                    # Ensure meg_grade is not None before trying to use it, and meg_points is not None
                    academic_megs[f"aLevel_meg_grade_{label_suffix}"] = meg_grade if meg_grade is not None else "N/A"
                    academic_megs[f"aLevel_meg_points_{label_suffix}"] = meg_points if meg_points is not None else 0
            
            # The academic_summary list should now be populated by parse_subjects_from_profile_record 
            # which is called inside the new get_academic_profile. We then need to iterate it here to add points and MEGs.
            # The old logic for iterating field_3080 etc. is now inside parse_subjects_from_profile_record.
            # We now need to process the `academic_summary` list that came from `get_academic_profile`
            # to add points and detailed MEGs per subject.

            processed_academic_summary = []
            if isinstance(academic_summary, list):
                for subject_entry in academic_summary:
                    if isinstance(subject_entry, dict) and subject_entry.get("subject") and not subject_entry["subject"].startswith("Academic profile not found") and not subject_entry["subject"].startswith("No academic subjects parsed") :
                        exam_type = subject_entry.get("examType", "A Level")
                        norm_qual = normalize_qualification_type(exam_type)
                        current_grade = subject_entry.get("currentGrade", "N/A")
                        
                        current_points = get_points(current_grade, norm_qual) if current_grade != 'N/A' else 0
                        subject_entry['normalized_qualification_type'] = norm_qual
                        subject_entry['currentGradePoints'] = current_points
                        
                        standard_meg_grade, standard_meg_points_val = "N/A", 0
                        if prior_attainment_score is not None:
                            # Get details for MEG lookup if needed (e.g. BTEC year/size)
                            qual_details_for_meg = extract_qual_details(exam_type, norm_qual, app.logger)
                            # The get_meg_for_prior_attainment in tutorapp also takes qual_details. We might need to adapt it or this call.
                            # For now, assuming the student version of get_meg_for_prior_attainment primarily uses percentile for A-Level
                            # and might need future enhancement for detailed non-Alevel MEG lookup based on qual_details.
                            standard_meg_grade, standard_meg_points_val = get_meg_for_prior_attainment(prior_attainment_score, norm_qual, 75) # Default 75th for standard
                        
                        subject_entry['standard_meg'] = standard_meg_grade if standard_meg_grade is not None else "N/A"
                        subject_entry['standardMegPoints'] = standard_meg_points_val if standard_meg_points_val is not None else 0
                        
                        if norm_qual == "A Level" and prior_attainment_score is not None:
                            for percentile in [60, 90, 100]: # 75th is already standard_meg
                                meg_grade_p, meg_points_p = get_meg_for_prior_attainment(prior_attainment_score, norm_qual, percentile)
                                if meg_points_p is not None:
                                    subject_entry[f"megPoints{percentile}"] = meg_points_p
                        processed_academic_summary.append(subject_entry)
                    else: # if subject entry is not valid, still add it to maintain list structure if it was a placeholder
                        processed_academic_summary.append(subject_entry)
                academic_summary = processed_academic_summary # Replace with the processed list

        else: # if object112_profile_record_data is None (academic profile not found by any method)
            app.logger.warning(f"No Object_112 data for student {student_name_from_obj3}. Academic summary will be default.")
            # academic_summary will be the default from get_academic_profile e.g. [{"subject": "Academic profile not found..."}]
            # Ensure it's a list for consistency
            if not isinstance(academic_summary, list) or not academic_summary: # Ensure academic_summary is the default list if record was None
                academic_summary = [{"subject": "Academic profile not found by any method.", "currentGrade": "N/A", "targetGrade": "N/A", "effortGrade": "N/A", "examType": "N/A"}]


        # Generate LLM insights
        # Ensure all necessary data for the LLM is included in this dictionary
        llm_data_for_insights = {
            "student_name": student_name_from_obj3,
            "student_level": object10_data.get("field_568_raw", "N/A") if object10_data else "N/A", # Add student_level
            "current_cycle": current_cycle,
            "vespa_profile": vespa_scores_for_profile, # This should be the dict with score_1_to_10 and score_profile_text
            "school_vespa_averages": school_vespa_averages, # Add school averages
            "academic_profile_summary": academic_summary, # List of subject dicts
            "academic_megs": academic_megs, # Dict of overall MEGs
            "student_reflections_and_goals": student_reflections,
            "object29_question_highlights": object29_highlights_top_bottom, # Dict with top_3 and bottom_3 lists
            "all_scored_questionnaire_statements": all_scored_statements # List of all scored statements for distribution calculation
        }
        
        # Pass app.logger to the LLM function
        llm_insights = generate_student_insights_with_llm(llm_data_for_insights, app.logger)
        
        # Use LLM insights if available, otherwise fall back to placeholders
        if not llm_insights:
            llm_insights = {
                "student_overview_summary": f"Welcome {student_name_from_obj3}! Your VESPA profile shows unique strengths and opportunities for growth. Let's explore them together.",
                "chart_comparative_insights": "Your VESPA scores show your current learning approach. Compare them with the school average to see where you stand.",
                "questionnaire_interpretation_and_reflection_summary": "Your questionnaire responses reveal important insights about your learning mindset and habits.",
                "academic_benchmark_analysis": "Your grades show your current performance. The MEG benchmarks indicate what's possible with focused effort.",
                "suggested_student_goals": [
                    "Focus on improving your lowest VESPA score this week",
                    "Set a specific study goal for your most challenging subject",
                    "Track your progress daily using a simple journal"
                ]
            }

        final_response = {
            "student_name": student_name_from_obj3,
            "student_level": object10_data.get("field_568_raw", "N/A") if object10_data else "N/A",
            "current_cycle": current_cycle,
            "vespa_profile": vespa_scores_for_profile,
            "academic_profile_summary": academic_summary,
            "academic_megs": academic_megs,
            "student_reflections_and_goals": student_reflections,
            "object29_question_highlights": object29_highlights_top_bottom,
            "llm_generated_insights": llm_insights, 
            "all_scored_questionnaire_statements": all_scored_statements,
            "school_vespa_averages": school_vespa_averages
        }
        return jsonify(final_response), 200

@app.route('/api/v1/chat_turn', methods=['POST', 'OPTIONS'])
def chat_turn():
    app.logger.info(f"Received request for /api/v1/chat_turn. Method: {request.method}")
    if request.method == 'OPTIONS':
        return _build_cors_preflight_response()
    
    if request.method == 'POST':
        data = request.get_json()
        app.logger.info(f"Chat turn data received: {str(data)[:500]}...") # Log first 500 chars

        # Student context: student_knack_id is the student's Object_3 ID.
        student_object3_id = data.get('student_knack_id') 
        chat_history = data.get('chat_history', []) 
        current_user_message = data.get('current_user_message')
        # initial_ai_context might contain the rich data from student_coaching_data endpoint
        initial_ai_context = data.get('initial_ai_context') 
        context_type = data.get('context_type', 'student') # Should be 'student'

        if not student_object3_id or not current_user_message:
            app.logger.error("chat_turn: Missing student_knack_id (Object_3 ID) or current_user_message.")
            return jsonify({"error": "Missing student ID or message"}), 400
        
        if not OPENAI_API_KEY:
            app.logger.error("chat_turn: OpenAI API key not configured.")
            # Save student message even if AI can't respond
            save_chat_message_to_knack(student_object3_id, "Student", current_user_message, is_student_chat=True)
            return jsonify({"ai_response": "I am currently unable to respond (AI not configured). Your message has been logged."}), 200

        # Save student's message to Knack
        user_message_saved_id = save_chat_message_to_knack(student_object3_id, "Student", current_user_message, is_student_chat=True)
        if not user_message_saved_id:
            app.logger.error(f"chat_turn: Failed to save student's message to Knack for student Object_3 ID {student_object3_id}.")
            # Proceed with AI response anyway, but log the failure.

        student_name_for_chat = "there" # Default
        if initial_ai_context and initial_ai_context.get('student_name'):
            student_name_for_chat = initial_ai_context['student_name'].split(' ')[0] # Use first name
        
        # --- LLM Prompt Construction for Student Chat ---
        messages_for_llm = [
            {"role": "system", "content": f"""You are 'My VESPA AI Coach', a friendly and supportive AI assistant for students.
            You are chatting with {student_name_for_chat}. Your goal is to help them understand their VESPA profile (Vision, Effort, Systems, Practice, Attitude), academic data, and questionnaire responses.
            Use encouraging language. Be clear and concise.
            If the student asks about a specific VESPA element or a challenge, try to provide actionable advice or suggest a relevant VESPA activity if one is provided in the context.
            When suggesting an activity:
            - Explain WHY it's relevant to what the student is asking or to their data.
            - Briefly describe WHAT the activity involves in a student-friendly way.
            - If a PDF link is mentioned for an activity in your context, you can say "There are resources to help you with this activity."
            - Only recommend activities that are explicitly provided to you in the '--- Available VESPA Activities ---' section of the RAG context. Do not invent activities. Use the activity's 'name' when referring to it.
            
            Keep your responses focused on the student's query and their data.
            Remember to be positive and empowering!"""
            }
        ]

        # --- RAG Context Building ---
        rag_context_parts = []
        suggested_activities_for_response = [] # To send back to frontend

        if initial_ai_context:
            rag_context_parts.append("--- About Me (Student's Data Summary) ---")
            if initial_ai_context.get('student_overview_summary'):
                rag_context_parts.append(f"My Overall AI Snapshot: {initial_ai_context['student_overview_summary']}")
            if initial_ai_context.get('vespa_profile'):
                rag_context_parts.append("My VESPA Scores:")
                for el, data_el in initial_ai_context['vespa_profile'].items():
                    if el != "Overall": # Overall is usually a summary score
                         rag_context_parts.append(f" - {el}: {data_el.get('score_1_to_10', 'N/A')}/10 ({data_el.get('score_profile_text', 'N/A')})")
            if initial_ai_context.get('suggested_student_goals'):
                 rag_context_parts.append(f"Some goals suggested for me earlier: {'; '.join(initial_ai_context['suggested_student_goals'])}")


        # Simple keyword extraction from student message for RAG
        # (Can be made more sophisticated later)
        if current_user_message:
            common_words = {"is", "a", "the", "and", "to", "of", "it", "in", "for", "on", "with", "as", "an", "at", "by", "my", "i", "me", "what", "how", "help", "can", "you"}
            cleaned_msg_for_kw = current_user_message.lower()
            for char_to_replace in ['?', '.', ',', '\'', '"', '!']:
                cleaned_msg_for_kw = cleaned_msg_for_kw.replace(char_to_replace, '')
            keywords = [word for word in cleaned_msg_for_kw.split() if word not in common_words and len(word) > 2]
            
            app.logger.info(f"Student chat RAG: Extracted keywords: {keywords} from message: '{current_user_message}'")

            # RAG for VESPA Activities
            if VESPA_ACTIVITIES_DATA and keywords: # VESPA_ACTIVITIES_DATA should be loaded globally
                found_activities_text_for_prompt = []
                processed_activity_ids_student_chat = set()
                
                # Simple match: activity name, summary, or VESPA element contains a keyword
                for activity in VESPA_ACTIVITIES_DATA:
                    activity_text_to_search = (
                        str(activity.get('name', '')).lower() +
                        str(activity.get('short_summary', '')).lower() +
                        str(activity.get('vespa_element', '')).lower() +
                        str(activity.get('keywords', [])).lower() # Include keywords from KB if present
                    )
                    activity_level = activity.get('level', '').lower() # handbook or level 2 / level 3
                    # Student level from initial_ai_context.get('student_level') can be 'Level 2' or 'Level 3'

                    # Basic keyword match for now, can add level filtering later
                    if any(kw in activity_text_to_search for kw in keywords):
                        if activity.get('id') not in processed_activity_ids_student_chat and len(suggested_activities_for_response) < 2: # Limit to 2 suggestions
                            activity_data_for_llm = {
                                "id": activity.get('id'),
                                "name": activity.get('name'),
                                "short_summary": activity.get('short_summary'),
                                "pdf_link": activity.get('pdf_link'),
                                "vespa_element": activity.get('vespa_element'),
                                "level": activity.get('level')
                            }
                            suggested_activities_for_response.append(activity_data_for_llm)
                            
                            pdf_available_text = " (Resource PDF available)" if activity_data_for_llm['pdf_link'] and activity_data_for_llm['pdf_link'] != '#' else ""
                            level_text = f" (Level: {activity_data_for_llm['level']})" if activity_data_for_llm['level'] else " (General)"
                            found_activities_text_for_prompt.append(
                                f"- Name: {activity_data_for_llm['name']}, VESPA Element: {activity_data_for_llm['vespa_element']}{level_text}{pdf_available_text}. Summary: {activity_data_for_llm['short_summary'][:100]}..."
                            )
                            processed_activity_ids_student_chat.add(activity_data_for_llm['id'])
                
                if found_activities_text_for_prompt:
                    rag_context_parts.append("\n--- Available VESPA Activities (Consider these if relevant to my question) ---")
                    rag_context_parts.extend(found_activities_text_for_prompt)
                    app.logger.info(f"Student chat RAG: Found {len(found_activities_text_for_prompt)} relevant VESPA activities for LLM.")
            
        if rag_context_parts:
            # Ensure the RAG context is properly formatted as a single string before inserting
            system_rag_content = "\n".join(rag_context_parts)
            messages_for_llm.insert(1, {"role": "system", "content": system_rag_content})
            app.logger.info(f"Student chat: Added RAG context to LLM prompt. Length: {len(system_rag_content)}")

        # Add chat history to messages_for_llm
        for message in chat_history:
            # Ensure role is 'user' or 'assistant'
            role = message.get("role", "user").lower()
            if role not in ["user", "assistant"]:
                role = "user" if sender == "Student" else "assistant" # Fallback based on common senders
            messages_for_llm.append({"role": role, "content": message.get("content", "")})
        
        # Add current student message
        messages_for_llm.append({"role": "user", "content": current_user_message})

        ai_response_text = "Sorry, I had a little trouble thinking of a response. Could you try asking in a different way?"
        try:
            app.logger.info(f"Student chat: Sending to LLM. Number of messages: {len(messages_for_llm)}.")
            
            llm_response = openai.chat.completions.create(
                model="gpt-3.5-turbo",
                messages=messages_for_llm,
                max_tokens=300, 
                temperature=0.7, # Slightly more creative for student chat
                n=1,
                stop=None
            )
            ai_response_text = llm_response.choices[0].message.content.strip()
            app.logger.info(f"Student chat: LLM raw response: {ai_response_text}")

        except Exception as e:
            app.logger.error(f"Student chat: Error calling OpenAI API: {e}")
            # ai_response_text remains the default error message

        # Save AI's response to Knack
        ai_message_saved_id = save_chat_message_to_knack(student_object3_id, "My AI Coach", ai_response_text, is_student_chat=True)
        if not ai_message_saved_id:
            app.logger.error(f"Student chat: Failed to save AI's response to Knack for student Object_3 ID {student_object3_id}.")

        return jsonify({
            "ai_response": ai_response_text, 
            "suggested_activities_in_chat": suggested_activities_for_response, # Send activities to frontend
            "ai_message_knack_id": ai_message_saved_id 
        })

@app.route('/api/v1/chat_history', methods=['POST', 'OPTIONS'])
def chat_history():
    app.logger.info(f"Received request for /api/v1/chat_history. Method: {request.method}")
    if request.method == 'OPTIONS':
        return _build_cors_preflight_response()
    
    if request.method == 'POST':
        data = request.get_json()
        student_object3_id = data.get('student_knack_id') # student_knack_id is Object_3 ID
        max_messages = data.get('max_messages', 50) # Default to 50

        if not student_object3_id:
            app.logger.error("get_chat_history: Missing student_knack_id (Object_3 ID).")
            return jsonify({"error": "Missing student_knack_id"}), 400

        knack_object_key_chatlog = "object_118"
        # We need to filter by field_3274 (Student connection to Object_3)
        filters = [
            {'field': 'field_3274', 'operator': 'is', 'value': student_object3_id}
        ]
        
        app.logger.info(f"Fetching chat history for student Obj3 ID {student_object3_id} from {knack_object_key_chatlog} with filters: {filters}")
        
        # Fetch all chat records for this student, up to a reasonable limit to sort then slice
        # Knack's API limit is 1000 per page.
        # Let's fetch up to max_messages * 2 (or 100 if max_messages is small) to allow for sorting.
        rows_to_fetch = max(100, max_messages * 2)
        if rows_to_fetch > 1000: rows_to_fetch = 1000

        chat_log_response = get_knack_record(
            knack_object_key_chatlog, 
            filters=filters, 
            page=1, 
            rows_per_page=rows_to_fetch 
        )

        all_student_chat_records = []
        if chat_log_response and isinstance(chat_log_response, dict) and 'records' in chat_log_response:
            all_student_chat_records = chat_log_response['records']
            app.logger.info(f"Fetched initial {len(all_student_chat_records)} chat records for student {student_object3_id}.")
        else:
            app.logger.warning(f"No chat records found or unexpected response format for student {student_object3_id}. Response: {chat_log_response}")
            return jsonify({"chat_history": [], "total_count": 0, "liked_count": 0, "summary": "No chat history found for you yet."}), 200

        # Sort records by timestamp (field_3276) - Knack format is 'dd/mm/yyyy HH:MM:SS'
        def get_datetime_from_knack_ts(ts_str):
            if not ts_str: return datetime.min
            try:
                return datetime.strptime(ts_str, '%d/%m/%Y %H:%M:%S')
            except ValueError:
                app.logger.warning(f"Could not parse Knack timestamp: {ts_str}. Using fallback for sorting.")
                return datetime.min

        all_student_chat_records.sort(key=lambda r: get_datetime_from_knack_ts(r.get('field_3276')), reverse=True)

        # Slice to get the actual max_messages
        recent_chat_records = all_student_chat_records[:max_messages]
        
        chat_history_for_frontend = []
        liked_count = 0 # Placeholder, 'liked' status not fully implemented in student chat UI/backend yet
        for record in recent_chat_records:
            # Determine role based on field_3273 (Author)
            author = record.get('field_3273', 'Student') # Default to student if author missing
            role_for_frontend = "assistant" if author == "My AI Coach" else "user"

            chat_history_for_frontend.append({
                "id": record.get('id'),
                "role": role_for_frontend,
                "content": record.get('field_3277', ""), # Message Text
                "is_liked": record.get('field_3279') == "Yes", # Liked status from field_3279
                "timestamp": record.get('field_3276')
            })
        
        # Reverse to have chronological order for display (oldest of the recent batch first)
        chat_history_for_frontend.reverse()

        total_chat_count_for_student = len(all_student_chat_records) # Count before slicing

        # Placeholder for summary, can be enhanced later
        summary_text = f"You have {total_chat_count_for_student} messages in your chat history."
        if initial_ai_context and initial_ai_context.get('student_overview_summary'): # Use initial context if available
             summary_text = initial_ai_context.get('student_overview_summary')


        app.logger.info(f"Returning {len(chat_history_for_frontend)} messages for student chat history. Total for student: {total_chat_count_for_student}.")
        return jsonify({
            "chat_history": chat_history_for_frontend,
            "total_count": total_chat_count_for_student,
            "liked_count": liked_count, # Placeholder
            "summary": summary_text # Placeholder
        }), 200


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

# --- Add definitions for new KBs from tutorapp.py here, after existing KB loading ---
coaching_kb = load_json_file('coaching_questions_knowledge_base.json')
COACHING_INSIGHTS_DATA = load_json_file('coaching_insights.json')
VESPA_ACTIVITIES_DATA = load_json_file('vespa_activities_kb.json')

# Load ALPS bands (ensure these JSON files are in your student app's knowledge_base directory)
alps_bands_btec2010_kb = load_json_file('alpsBands_btec2010_main.json')
alps_bands_btec2016_kb = load_json_file('alpsBands_btec2016_main.json')
alps_bands_cache_kb = load_json_file('alpsBands_cache.json')
alps_bands_ib_kb = load_json_file('alpsBands_ib.json')
alps_bands_preU_kb = load_json_file('alpsBands_preU.json')
alps_bands_ual_kb = load_json_file('alpsBands_ual.json')
alps_bands_wjec_kb = load_json_file('alpsBands_wjec.json')

# Load Reflective Statements from text file (similar to tutorapp.py)
REFLECTIVE_STATEMENTS_DATA = []
try:
    current_dir = os.path.dirname(os.path.abspath(__file__))
    # Path should be relative to this app.py, inside knowledge_base.
    # If '100 statements - 2023.txt' is directly in 'knowledge_base' for student app:
    statements_file_path = os.path.join(current_dir, 'knowledge_base', '100 statements - 2023.txt')
    statements_file_path = os.path.normpath(statements_file_path)
    app.logger.info(f"Attempting to load 100 statements from: {statements_file_path}")
    with open(statements_file_path, 'r', encoding='utf-8') as f:
        REFLECTIVE_STATEMENTS_DATA = [line.strip() for line in f if line.strip()]
    app.logger.info(f"Successfully loaded {len(REFLECTIVE_STATEMENTS_DATA)} statements.")
except FileNotFoundError:
    app.logger.error(f"'100 statements - 2023.txt' not found at {statements_file_path}.")
    # Consider if this should be a critical error or if the app can run without it.
except Exception as e:
    app.logger.error(f"Error loading '100 statements - 2023.txt': {e}")

# Log status of newly loaded KBs
if not coaching_kb: app.logger.warning("Coaching KB (coaching_questions_knowledge_base.json) failed to load.")
else: app.logger.info("Successfully loaded Coaching KB.")
if not COACHING_INSIGHTS_DATA: app.logger.warning("Coaching Insights KB (coaching_insights.json) failed to load.")
else: app.logger.info(f"Successfully loaded {len(COACHING_INSIGHTS_DATA)} records from Coaching Insights KB.")
if not VESPA_ACTIVITIES_DATA: app.logger.warning("VESPA Activities KB (vespa_activities_kb.json) failed to load.")
else: app.logger.info(f"Successfully loaded {len(VESPA_ACTIVITIES_DATA)} records from VESPA Activities KB.")
if not REFLECTIVE_STATEMENTS_DATA: app.logger.warning("Reflective Statements (100_statements.txt) failed to load or is empty.")
else: app.logger.info(f"Successfully loaded {len(REFLECTIVE_STATEMENTS_DATA)} statements from 100_statements.txt")

# Existing ALPS KBs checks (just to ensure we don't duplicate logs if they existed, but adding new ones)
if not alps_bands_aLevel_60_kb: app.logger.warning("ALPS A-Level 60th percentile KB failed to load.")
if not alps_bands_aLevel_75_kb: app.logger.warning("ALPS A-Level 75th percentile KB failed to load.") # Already loaded, but good to check
if not alps_bands_aLevel_90_kb: app.logger.warning("ALPS A-Level 90th percentile KB failed to load.")
if not alps_bands_aLevel_100_kb: app.logger.warning("ALPS A-Level 100th percentile KB failed to load.")
if not alps_bands_btec2010_kb: app.logger.warning("ALPS BTEC 2010 KB failed to load.")
if not alps_bands_btec2016_kb: app.logger.warning("ALPS BTEC 2016 KB failed to load.")
# Add checks for cache, ib, preU, ual, wjec if desired

# Ensure all required KBs for core functionality are checked critically
if not grade_points_mapping_kb: app.logger.error("CRITICAL: Grade Points Mapping KB failed to load.")
if not psychometric_question_details_kb: app.logger.warning("Psychometric Question Details KB failed to load.")
if not report_text_kb: app.logger.warning("Report Text KB (Object_33) failed to load.")

if __name__ == '__main__':
    # For local development. Heroku uses Procfile.
    port = int(os.environ.get('PORT', 5002)) # Use a different port than tutor coach if running locally
    app.run(debug=True, port=port, host='0.0.0.0') 