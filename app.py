import asyncio
import base64
import json
import os
from collections import deque
from typing import Dict

import dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi import FastAPI, HTTPException, Request  # Correct import for Request
from fastapi.responses import HTMLResponse
from twilio.rest import Client
from twilio.twiml.voice_response import Connect, VoiceResponse
import redis

from logger_config import get_logger
from services.call_context import CallContext
from services.llm_service import LLMFactory
from services.stream_service import StreamService
from services.transcription_service import TranscriptionService
from services.tts_service import TTSFactory
from fastapi.staticfiles import StaticFiles
from fastapi import FastAPI, UploadFile, File
import shutil
import csv

dotenv.load_dotenv()
app = FastAPI()
logger = get_logger("App")

# Serve static files
# Set the absolute path to the static directory
static_dir = r"F:\Python\aidialer-main\statics"

# Serve static files from the specified directory
app.mount("/static", StaticFiles(directory=static_dir), name="static")

# Initialize Redis connection
# Create a Redis connection pool
redis_pool = redis.ConnectionPool(host='localhost', port=6379, db=0)
redis_client = redis.Redis(connection_pool=redis_pool)

# Path to save the uploaded CSV files
UPLOAD_FOLDER = "./uploads/"
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# Global dictionary to store call contexts for each server instance (should be replaced with a database in production)
global call_contexts
call_contexts = {}

# Function to delete existing files in the uploads folder
def clear_upload_folder():
    """Deletes all the files in the upload folder before saving a new one."""
    for filename in os.listdir(UPLOAD_FOLDER):
        file_path = os.path.join(UPLOAD_FOLDER, filename)
        try:
            if os.path.isfile(file_path) or os.path.islink(file_path):
                os.remove(file_path)
            else:
                print(f"{file_path} is not a file or a symlink.")
        except Exception as e:
            print(f"Failed to delete {file_path}. Reason: {e}")

# Route to upload CSV file
@app.post("/upload_csv/")
async def upload_csv(file: UploadFile = File(...)):
    # Clear the folder first to remove any existing file
    clear_upload_folder()

    # Save the new file with the original filename
    file_location = os.path.join(UPLOAD_FOLDER, file.filename)
    with open(file_location, "wb") as f:
        shutil.copyfileobj(file.file, f)

    return {"message": "File uploaded successfully", "filename": file.filename}


# Route to check if a CSV file exists and return its name
@app.get("/current_csv/")
async def get_current_csv():
    files = os.listdir(UPLOAD_FOLDER)
    
    if files:
        # Return the first file found in the folder (since we only allow one at a time)
        return {"file_exists": True, "filename": files[0]}
    else:
        return {"file_exists": False}

# Route to read the contents of the current CSV file and return the count of numbers
@app.get("/read_csv/")
async def read_csv():
    files = os.listdir(UPLOAD_FOLDER)
    
    if files:
        file_path = os.path.join(UPLOAD_FOLDER, files[0])
        contacts = []
        with open(file_path, "r") as f:
            reader = csv.reader(f)
            next(reader)  # Skip header
            for row in reader:
                if len(row) >= 2:  # Ensure there's a name and phone number
                    contacts.append({"name": row[0], "phone_number": row[1]})
        
        total_contacts = len(contacts)
        return {
            "file_name": files[0], 
            "contacts": contacts, 
            "total_contacts": total_contacts
        }
    else:
        return {"error": "No file found"}


# Function to save CallContext in Redis
def save_call_context(call_sid, call_context):
    # Create a dictionary representation of CallContext
    context_dict = {
        "system_message": getattr(call_context, "system_message", None),
        "initial_message": getattr(call_context, "initial_message", None),
        "call_sid": getattr(call_context, "call_sid", None),
        "user_context": getattr(call_context, "user_context", None),  # Assuming CallContext has this attribute
        "transfer_number": getattr(call_context, "transfer_number", None)  # Add transfer_number here
    }
    redis_client.set(f"call_context:{call_sid}", json.dumps(context_dict))


# Function to get CallContext from Redis
def get_call_context(call_sid):
    data = redis_client.get(f"call_context:{call_sid}")
    if data:
        call_data = json.loads(data)
        # Create a blank CallContext instance
        call_context = CallContext()
        
        # Dynamically set the attributes
        setattr(call_context, "system_message", call_data.get("system_message"))
        setattr(call_context, "initial_message", call_data.get("initial_message"))
        setattr(call_context, "call_sid", call_data.get("call_sid"))
        setattr(call_context, "user_context", call_data.get("user_context"))  # Assuming CallContext has this attribute
        setattr(call_context, "transfer_number", call_data.get("transfer_number"))  # Retrieve transfer_number

        return call_context
    return None


# Function to delete CallContext from Redis
def delete_call_context(call_sid):
    redis_client.delete(f"call_context:{call_sid}")

# First route that gets called by Twilio when call is initiated
@app.post("/incoming")
async def incoming_call() -> HTMLResponse:
    server = os.environ.get("SERVER")
    response = VoiceResponse()
    connect = Connect()
    connect.stream(url=f"wss://{server}/connection")
    response.append(connect)
    return HTMLResponse(content=str(response), status_code=200)

@app.get("/call_recording/{call_sid}")
async def get_call_recording(call_sid: str):
    """Get the recording URL and transcription for a specific call."""
    client = get_twilio_client()
    recordings = client.calls(call_sid).recordings.list()
    if recordings:
        recording = recordings[0]
        transcription = client.recordings(recording.sid).transcriptions.list()
        if transcription:
            return {"recording_url": f"https://api.twilio.com{recording.uri}", "transcription": transcription[0].transcription_text}
        return {"recording_url": f"https://api.twilio.com{recording.uri}", "transcription": "No transcription available"}
    return {"error": "Recording not found"}


# Websocket route for Twilio to get media stream
@app.websocket("/connection")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()

    llm_service_name = os.getenv("LLM_SERVICE", "openai")
    tts_service_name = os.getenv("TTS_SERVICE", "deepgram")

    logger.info(f"Using LLM service: {llm_service_name}")
    logger.info(f"Using TTS service: {tts_service_name}")

    llm_service = LLMFactory.get_llm_service(llm_service_name, CallContext())
    stream_service = StreamService(websocket)
    transcription_service = TranscriptionService()
    tts_service = TTSFactory.get_tts_service(tts_service_name)
    
    marks = deque()
    interaction_count = 0

    await transcription_service.connect()

    async def process_media(msg):
        await transcription_service.send(base64.b64decode(msg['media']['payload']))

    async def handle_transcription(text):
        nonlocal interaction_count
        if not text:
            return
        logger.info(f"Interaction {interaction_count} – STT -> LLM: {text}")
        await llm_service.completion(text, interaction_count)
        interaction_count += 1

    async def handle_llm_reply(llm_reply, icount):
        logger.info(f"Interaction {icount}: LLM -> TTS: {llm_reply['partialResponse']}")
        await tts_service.generate(llm_reply, icount)

    async def handle_speech(response_index, audio, label, icount):
        logger.info(f"Interaction {icount}: TTS -> TWILIO: {label}")
        await stream_service.buffer(response_index, audio)

    async def handle_audio_sent(mark_label):
        marks.append(mark_label)

    async def handle_utterance(text, stream_sid):
        try:
            if len(marks) > 0 and text.strip():
                logger.info("Interruption detected, clearing system.")
                await websocket.send_json({
                    "streamSid": stream_sid,
                    "event": "clear"
                })
                
                # reset states
                stream_service.reset()
                llm_service.reset()

        except Exception as e:
            logger.error(f"Error while handling utterance: {e}")
            e.print_stack()

    transcription_service.on('utterance', handle_utterance)
    transcription_service.on('transcription', handle_transcription)
    llm_service.on('llmreply', handle_llm_reply)
    tts_service.on('speech', handle_speech)
    stream_service.on('audiosent', handle_audio_sent)

    # Queue for incoming WebSocket messages
    message_queue = asyncio.Queue()

    async def websocket_listener():
        try:
            while True:
                data = await websocket.receive_text()
                await message_queue.put(json.loads(data))
        except WebSocketDisconnect:
            logger.info("WebSocket disconnected")

    async def message_processor():
        while True:
            msg = await message_queue.get()
            if msg['event'] == 'start':
                stream_sid = msg['start']['streamSid']
                call_sid = msg['start']['callSid']

                # Retrieve from Redis instead of in-memory
                call_context = get_call_context(call_sid)

                if os.getenv("RECORD_CALLS") == "true":
                    get_twilio_client().calls(call_sid).recordings.create({"recordingChannels": "dual"})

                # Decide if the call was initiated from the UI or is an inbound
                if not call_context:
                    # Inbound call
                    call_context = CallContext()
                    call_context.system_message = os.environ.get("SYSTEM_MESSAGE")
                    call_context.initial_message = os.environ.get("INITIAL_MESSAGE")
                    call_context.call_sid = call_sid
                    call_context.transfer_number = os.environ.get("TRANSFER_NUMBER")  # Set the transfer number here
                    save_call_context(call_sid, call_context)
                llm_service.set_call_context(call_context)

                stream_service.set_stream_sid(stream_sid)
                transcription_service.set_stream_sid(stream_sid)

                logger.info(f"Twilio -> Starting Media Stream for {stream_sid}")
                await tts_service.generate({
                    "partialResponseIndex": None,
                    "partialResponse": call_context.initial_message
                }, 1)
            elif msg['event'] == 'media':
                asyncio.create_task(process_media(msg))
            elif msg['event'] == 'mark':
                label = msg['mark']['name']
                if label in marks:
                    marks.remove(label)
            elif msg['event'] == 'stop':
                logger.info(f"Twilio -> Media stream {stream_sid} ended.")
                break
            message_queue.task_done()

    try:
        listener_task = asyncio.create_task(websocket_listener())
        processor_task = asyncio.create_task(message_processor())

        await asyncio.gather(listener_task, processor_task)
    except asyncio.CancelledError:
        logger.info("Tasks cancelled")
    finally:
        await transcription_service.disconnect()

def get_twilio_client():
    return Client(os.getenv("TWILIO_ACCOUNT_SID"), os.getenv("TWILIO_AUTH_TOKEN"))

# API route to initiate a call via UI
from typing import List, Dict
from fastapi import Body

# API route to initiate a call via UI
@app.post("/start_call")
async def start_call(
    to_numbers: List[str] = Body(...),
    system_message: str = Body(None),
    initial_message: str = Body(None),
    transfer_number: str = Body(...),
    twilio_number: str = Body(...)  # Add the Twilio number in the request body
):
    if not to_numbers:
        return {"error": "Missing 'to_numbers' in request"}

    if not transfer_number or not twilio_number:
        return {"error": "Missing 'transfer_number' or 'twilio_number' in request"}

    service_url = f"https://{os.getenv('SERVER')}/incoming"
    responses = []

    for to_number in to_numbers:
        try:
            client = get_twilio_client()
            logger.info(f"Initiating call to {to_number} from {twilio_number} via {service_url}")
            call = client.calls.create(
                to=to_number,
                from_=twilio_number,  # Use the selected Twilio number here
                url=service_url,
                record=True,
                recording_status_callback=f"{service_url}/recording-status",
                timeout=55
            )

            call_sid = call.sid
            logger.info(f"Call initiated successfully with call_sid: {call_sid}")

            # Save call context
            call_context = CallContext()
            call_context.system_message = system_message or os.getenv("SYSTEM_MESSAGE")
            call_context.initial_message = initial_message or os.getenv("INITIAL_MESSAGE")
            call_context.call_sid = call_sid
            call_context.transfer_number = transfer_number
            save_call_context(call_sid, call_context)

            # Append the call_sid to the responses
            responses.append({"call_sid": call_sid, "to_number": to_number})
        except Exception as e:
            logger.error(f"Error initiating call to {to_number}: {str(e)}")
            responses.append({"error": f"Failed to initiate call to {to_number}: {str(e)}"})

    return {"results": responses}





from fastapi import HTTPException

# API route to get the status of a call
@app.get("/call_status/{call_sid}")
async def get_call_status(call_sid: str):
    """Get the status of a call."""
    try:
        client = get_twilio_client()
        call = client.calls(call_sid).fetch()
        return {"status": call.status}
    except Exception as e:
        logger.error(f"Error fetching call status: {str(e)}")
        return {"error": f"Failed to fetch call status: {str(e)}"}

# API route to end a call
@app.post("/end_call")
async def end_call(request: Dict[str, str]):
    """End an ongoing call."""
    try:
        call_sid = request.get("call_sid")
        client = get_twilio_client()
        client.calls(call_sid).update(status='completed')
        delete_call_context(call_sid)
        return {"status": "success"}
    except Exception as e:
        logger.error(f"Error ending call {str(e)}")
        return {"error": f"Failed to end requested call: {str(e)}"}

# API call to get the transcript for a specific call
@app.get("/transcript/{call_sid}")
async def get_transcript(call_sid: str):
    """Get the entire transcript for a specific call."""
    call_context = call_contexts.get(call_sid)

    if not call_context:
        logger.info(f"[GET] Call not found for call SID: {call_sid}")
        return {"error": "Call not found"}

    return {"transcript": call_context.user_context}


from fastapi import Form

@app.post("/incoming/recording-status")
async def recording_status(RecordingSid: str = Form(...), CallSid: str = Form(...)):
    logger.info(f"RecordingSid: {RecordingSid}, CallSid: {CallSid}")

    # Request transcription for the recording
    client = get_twilio_client()
    
    try:
        transcription = client.recordings(RecordingSid).transcriptions.create(
            language="en-US"  # Specify the transcription language
        )
        logger.info(f"Transcription requested for RecordingSid: {RecordingSid}")
    except Exception as e:
        logger.error(f"Error requesting transcription: {str(e)}")
    
    return {"status": "received"}


@app.get("/check_transcription/{recording_sid}")
async def check_transcription(recording_sid: str):
    client = get_twilio_client()
    transcription = client.recordings(recording_sid).transcriptions.list()
    
    if transcription:
        return {"status": transcription[0].status, "transcription": transcription[0].transcription_text}
    else:
        return {"error": "No transcription available"}


# API route to get all call transcripts
@app.get("/all_transcripts")
async def get_all_transcripts():
    """Get a list of all current call transcripts."""
    try:
        transcript_list = []
        for call_sid, context in call_contexts.items():
            transcript_list.append({
                "call_sid": call_sid,
                "transcript": context.user_context,
            })
        return {"transcripts": transcript_list}
    except Exception as e:
        logger.error(f"Error fetching all transcripts: {str(e)}")
        return {"error": f"Failed to fetch all transcripts: {str(e)}"}


TRANSFER_NUMBERS_FILE = r"F:\Python\aidialer-main\transfernumbers.json"

# Function to read transfer numbers from file
def read_transfer_numbers():
    try:
        if os.path.exists(TRANSFER_NUMBERS_FILE):
            with open(TRANSFER_NUMBERS_FILE, "r") as file:
                content = file.read().strip()  # Read and strip any extra whitespace
                if content:  # Check if the file is not empty
                    return json.loads(content)
                else:
                    return []  # Return an empty list if the file is empty
        return []  # Return an empty list if the file does not exist
    except json.JSONDecodeError as e:
        logger.error(f"Error decoding JSON from {TRANSFER_NUMBERS_FILE}: {str(e)}")
        return []  # Return an empty list if JSON decoding fails

# Function to save transfer numbers to file
def write_transfer_numbers(data):
    try:
        with open(TRANSFER_NUMBERS_FILE, "w") as file:
            json.dump(data, file, indent=4)
    except Exception as e:
        logger.error(f"Error writing to {TRANSFER_NUMBERS_FILE}: {str(e)}")


@app.post("/save_transfer_number")
async def save_transfer_number(request: Request):
    try:
        # Parse the request body as JSON
        body = await request.json()
        logger.info(f"Received request body: {body}")

        name = body.get('name')
        phone_number = body.get('phone_number')

        if not name or not phone_number:
            raise HTTPException(status_code=400, detail="Name and phone number are required")

        transfer_numbers = read_transfer_numbers()

        # Check if the phone number already exists
        for number in transfer_numbers:
            if number["phone_number"] == phone_number:
                raise HTTPException(status_code=400, detail="Phone number already exists")

        # Add new transfer number
        transfer_numbers.append({"name": name, "phone_number": phone_number})
        write_transfer_numbers(transfer_numbers)

        return {"success": True}

    except json.JSONDecodeError as e:
        logger.error(f"JSONDecodeError: {str(e)}")
        raise HTTPException(status_code=400, detail="Invalid JSON data")
    except Exception as e:
        logger.error(f"Error saving transfer number: {str(e)}")
        raise HTTPException(status_code=500, detail="Internal Server Error")

    
# API to get saved transfer numbers
@app.get("/get_transfer_numbers")
async def get_transfer_numbers():
    transfer_numbers = read_transfer_numbers()
    return {"numbers": transfer_numbers}

# API to delete a transfer number
@app.delete("/delete_transfer_number/{phone_number}")
async def delete_transfer_number(phone_number: str):
    transfer_numbers = read_transfer_numbers()
    updated_numbers = [num for num in transfer_numbers if num["phone_number"] != phone_number]

    if len(transfer_numbers) == len(updated_numbers):
        raise HTTPException(status_code=404, detail="Phone number not found")

    write_transfer_numbers(updated_numbers)
    return {"success": True}


# Function to get the most recent CSV file in the uploads folder
def get_latest_csv_file(upload_folder):
    try:
        # List all files in the upload folder
        files = [f for f in os.listdir(upload_folder) if f.endswith('.csv')]
        
        if not files:
            raise FileNotFoundError("No CSV files found in the uploads folder.")
        
        # Select the most recent file based on modification time
        latest_file = max(files, key=lambda f: os.path.getmtime(os.path.join(upload_folder, f)))
        return os.path.join(upload_folder, latest_file)
    except Exception as e:
        raise FileNotFoundError(f"Error finding CSV file: {str(e)}")

# Function to read contacts from the latest CSV file
def read_csv_file(file_path):
    contacts = []
    with open(file_path, newline='') as csvfile:
        reader = csv.reader(csvfile)
        next(reader)  # Skip header if it exists
        for row in reader:
            if len(row) >= 2:  # Ensure there's a name and phone number
                contacts.append({"name": row[0], "phone_number": row[1]})
    return contacts

# Function to write contacts back to the CSV after removing processed ones
def write_csv_file(file_path, contacts):
    with open(file_path, 'w', newline='') as csvfile:
        writer = csv.writer(csvfile)
        writer.writerow(['Name', 'Phone Number'])  # Assuming the CSV has a header
        for contact in contacts:
            writer.writerow([contact['name'], contact['phone_number']])

@app.post("/start_bulk_calls")
async def start_bulk_calls(
    num_calls: int = Body(...),  # Number of calls to make
    transfer_number: str = Body(...),
    twilio_number: str = Body(...),  # Add the Twilio number in the request body
    system_message: str = Body(None),
    initial_message: str = Body(None)
):
    """Initiate bulk calls using Twilio and update the CSV."""

    try:
        # Get the latest CSV file from the uploads folder
        csv_file_path = get_latest_csv_file(UPLOAD_FOLDER)

        # Read the contacts from the CSV file
        contacts = read_csv_file(csv_file_path)

        if num_calls > len(contacts):
            return {"error": f"Not enough contacts in the CSV file. You requested {num_calls}, but only {len(contacts)} contacts are available."}

        to_call_contacts = contacts[:num_calls]  # Select the top N contacts based on num_calls
        service_url = f"https://{os.getenv('SERVER')}/incoming"
        responses = []

        for contact in to_call_contacts:
            try:
                client = get_twilio_client()
                logger.info(f"Initiating bulk call to {contact['phone_number']} from {twilio_number} via {service_url}")
                call = client.calls.create(
                    to=contact['phone_number'],
                    from_=twilio_number,  # Use the selected Twilio number here
                    url=service_url,
                    record=True,
                    recording_status_callback=f"{service_url}/recording-status",
                    timeout=55
                )

                call_sid = call.sid
                logger.info(f"Call initiated successfully with call_sid: {call_sid}")

                # Save call context
                call_context = CallContext()
                call_context.system_message = system_message or os.getenv("SYSTEM_MESSAGE")
                call_context.initial_message = initial_message or os.getenv("INITIAL_MESSAGE")
                call_context.call_sid = call_sid
                call_context.transfer_number = transfer_number
                save_call_context(call_sid, call_context)

                responses.append({"call_sid": call_sid, "to_number": contact['phone_number']})
            except Exception as e:
                logger.error(f"Error initiating call to {contact['phone_number']}: {str(e)}")
                responses.append({"error": f"Failed to initiate call to {contact['phone_number']}: {str(e)}"})

        # Remove the called contacts from the original CSV
        remaining_contacts = contacts[num_calls:]  # Remove the top N contacts that were just called
        write_csv_file(csv_file_path, remaining_contacts)

        return {"results": responses}
    except FileNotFoundError as e:
        return {"error": str(e)}


@app.get("/get_twilio_numbers")
async def get_twilio_numbers():
    try:
        client = get_twilio_client()
        incoming_phone_numbers = client.incoming_phone_numbers.list()
        
        # Create a list of all Twilio numbers
        twilio_numbers = [{"phone_number": number.phone_number} for number in incoming_phone_numbers]
        return {"numbers": twilio_numbers}
    except Exception as e:
        logger.error(f"Error fetching Twilio numbers: {str(e)}")
        return {"error": "Failed to fetch Twilio numbers"}



if __name__ == "__main__":
    import uvicorn
    logger.info("Starting server...")
    logger.info(f"Backend server address set to: {os.getenv('SERVER')}")
    port = int(os.getenv("PORT", 3000))
    uvicorn.run(app, host="0.0.0.0", port=port)
