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
    filters = [
        {'field': 'field_792', 'operator': 'is', 'value': object10_id}, # Connection to Object_10
        {'field': 'field_863_raw', 'operator': 'is', 'value': str(cycle_number)} # Cycle number
    ]
    response = get_knack_record("object_29", filters=filters)
    if response and response.get('records'):
        return response['records'][0] # Assuming one Object_29 per student per cycle
    app.logger.warning(f"No Object_29 data found for Object_10 ID {object10_id}, Cycle {cycle_number}.")
    return None

def get_student_academic_profile(student_object3_id):
    "Fetches Object_112 (Academic Profile) using student's Object_3 ID."
    if not student_object3_id: return None
    # field_3070 in Object_112 is the Account connection to Object_3
    filters = [{'field': 'field_3070', 'operator': 'is', 'value': student_object3_id}]
    response = get_knack_record("object_112", filters=filters)
    if response and response.get('records'):
        return response['records'][0]
    app.logger.warning(f"No Object_112 (Academic Profile) found for student Object_3 ID {student_object3_id}.")
    return None    

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
    
    # Use the correct filter from tutor app - field_133 is the school connection
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

# --- LLM Integration for Student Insights ---
def generate_student_insights_with_llm(student_data):
    """Generate personalized insights for students using OpenAI."""
    if not OPENAI_API_KEY:
        app.logger.warning("OpenAI API key not set. Returning placeholder insights.")
        return None
    
    try:
        openai.api_key = OPENAI_API_KEY
        
        # Build context for the LLM
        context = f"""
You are an AI coach helping a student understand their VESPA profile and academic performance. 
Generate encouraging, constructive insights tailored for the student themselves (not their tutor).

Student: {student_data.get('student_name', 'Student')}
Current Cycle: {student_data.get('current_cycle', 0)}

VESPA Profile:
{json.dumps(student_data.get('vespa_profile', {}), indent=2)}

Academic Summary:
{json.dumps(student_data.get('academic_profile_summary', []), indent=2)}

Student Reflections:
{json.dumps(student_data.get('student_reflections_and_goals', {}), indent=2)}

Questionnaire Highlights:
Top 3 responses: {json.dumps(student_data.get('object29_question_highlights', {}).get('top_3', []), indent=2)}
Bottom 3 responses: {json.dumps(student_data.get('object29_question_highlights', {}).get('bottom_3', []), indent=2)}
"""

        # Create the prompt
        prompt = f"""{context}

Based on this data, provide the following insights for the student:

1. **Student Overview Summary** (2-3 sentences): A personalized, encouraging summary highlighting their strengths and areas for growth.

2. **Chart Comparative Insights** (2-3 sentences): Help them understand what their VESPA scores mean compared to their school average (if available).

3. **Questionnaire Reflection** (3-4 sentences): Help them understand what their questionnaire responses reveal about their learning approach and mindset.

4. **Academic Benchmark Analysis** (2-3 sentences): Explain how their current grades compare to their potential (MEGs) in an encouraging way.

5. **Suggested Goals** (3 specific, actionable goals): Based on their profile, what should they focus on?

Format your response as a JSON object with these keys:
- student_overview_summary
- chart_comparative_insights
- questionnaire_interpretation_and_reflection_summary
- academic_benchmark_analysis
- suggested_student_goals (as an array of strings)

Be positive, specific, and actionable. Use "you" and "your" to speak directly to the student."""

        response = openai.ChatCompletion.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role": "system", "content": "You are a supportive AI coach helping students improve their learning through the VESPA framework (Vision, Effort, Systems, Practice, Attitude)."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.7,
            max_tokens=800
        )
        
        # Parse the response
        response_text = response.choices[0].message.content
        
        # Try to parse as JSON
        try:
            insights = json.loads(response_text)
            app.logger.info("Successfully generated LLM insights for student.")
            return insights
        except json.JSONDecodeError:
            # If not valid JSON, try to extract insights manually
            app.logger.warning("LLM response was not valid JSON. Using fallback parsing.")
            # Simple fallback - just return the text as overview
            return {
                "student_overview_summary": response_text[:200] + "...",
                "chart_comparative_insights": "Please review your VESPA scores to understand your strengths.",
                "questionnaire_interpretation_and_reflection_summary": "Your questionnaire responses provide insights into your learning approach.",
                "academic_benchmark_analysis": "Compare your current grades with your expected grades to identify areas for improvement.",
                "suggested_student_goals": ["Focus on your lowest VESPA score", "Set specific study goals", "Track your progress weekly"]
            }
            
    except Exception as e:
        app.logger.error(f"Error generating LLM insights: {e}")
        return None

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
                            "question_text": q_detail.get('questionText', 'Unknown Question'),
                            "score": score,
                            "vespa_category": q_detail.get('vespaCategory', 'N/A')
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
        
        object112_data = get_student_academic_profile(student_object3_id)
        if object112_data:
            app.logger.info(f"Fetched Object_112 data for student: {object112_data.get('field_3066')} (Name in Obj112)")
            
            # Get prior attainment score (field_3272)
            prior_attainment_raw = object112_data.get('field_3272_raw', object112_data.get('field_3272'))
            if prior_attainment_raw:
                try:
                    prior_attainment_score = float(prior_attainment_raw)
                    app.logger.info(f"Prior attainment score: {prior_attainment_score}")
                except (ValueError, TypeError):
                    app.logger.warning(f"Could not parse prior attainment score: {prior_attainment_raw}")
            
            # Calculate overall MEGs if prior attainment is available
            if prior_attainment_score is not None:
                academic_megs["prior_attainment_score"] = prior_attainment_score
                
                # Calculate A-Level MEGs at different percentiles
                for percentile, label in [(60, "60th"), (75, "75th"), (90, "90th"), (100, "100th")]:
                    meg_grade, meg_points = get_meg_for_prior_attainment(prior_attainment_score, "A Level", percentile)
                    if meg_grade:
                        academic_megs[f"aLevel_meg_grade_{label}"] = meg_grade
                        academic_megs[f"aLevel_meg_points_{label}"] = meg_points or 0
            
            # Process each subject
            for i in range(1, 16): # Sub1 to Sub15
                subject_json_str = object112_data.get(f"field_30{79+i}") # e.g. field_3080
                if subject_json_str and isinstance(subject_json_str, str) and subject_json_str.strip().startswith('{'):
                    try:
                        s_data = json.loads(subject_json_str)
                        subject_name = s_data.get('subject', f'Subject {i}')
                        exam_type = s_data.get('examType', 'A Level')
                        norm_qual = normalize_qualification_type(exam_type)
                        current_grade = s_data.get('currentGrade', 'N/A')
                        
                        # Calculate points for current grade
                        current_points = get_points(current_grade, norm_qual) if current_grade != 'N/A' else 0
                        
                        # Get standard MEG (75th percentile for A-Level, or default)
                        standard_meg, standard_meg_points = None, None
                        if prior_attainment_score is not None:
                            standard_meg, standard_meg_points = get_meg_for_prior_attainment(prior_attainment_score, norm_qual, 75)
                        
                        subject_entry = {
                            "subject": subject_name,
                            "currentGrade": current_grade,
                            "targetGrade": s_data.get('targetGrade', 'N/A'),
                            "effortGrade": s_data.get('effortGrade', 'N/A'),
                            "examType": exam_type,
                            "normalized_qualification_type": norm_qual,
                            "currentGradePoints": current_points,
                            "standard_meg": standard_meg or 'N/A',
                            "standardMegPoints": standard_meg_points or 0
                        }
                        
                        # For A-Levels, add percentile MEGs
                        if norm_qual == "A Level" and prior_attainment_score is not None:
                            for percentile in [60, 90, 100]:
                                meg_grade, meg_points = get_meg_for_prior_attainment(prior_attainment_score, norm_qual, percentile)
                                if meg_points is not None:
                                    subject_entry[f"megPoints{percentile}"] = meg_points
                        
                        academic_summary.append(subject_entry)
                        
                    except json.JSONDecodeError:
                        app.logger.warning(f"Could not parse subject JSON from field_30{79+i} in Object_112.")
        else:
            app.logger.warning(f"No Object_112 data for student {student_name_from_obj3}.")
            academic_summary.append({"subject": "Academic data not found.", "currentGrade": "N/A"})


        # Generate LLM insights
        llm_data_for_insights = {
            "student_name": student_name_from_obj3,
            "current_cycle": current_cycle,
            "vespa_profile": vespa_scores_for_profile,
            "academic_profile_summary": academic_summary,
            "student_reflections_and_goals": student_reflections,
            "object29_question_highlights": object29_highlights_top_bottom
        }
        
        llm_insights = generate_student_insights_with_llm(llm_data_for_insights)
        
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