import time
from venv import logger
from collections import defaultdict
import asyncio

EDP_STATES = ["INITIALIZING", "WAITING_FOR_FILE_UPLOAD", "WAITING_FOR_BILL_POSTING_COMPLETION", "WAITING_FOR_RECON_COMPLETION", "WAITING_FOR_CNG_COMPLETION", "WAITING_FOR_GTG", "SUCCEEDED", "FAILED"]


SEGMENTS = ["CASH","DR","CUR","SLBM","MCX","NCDEX","MTF","COL_VAL"]

atomic_loop_start_time = None
atomic_loop_end_time = None

def move_to_state(segment: str, new_state: str, remarks: str = None) -> None:
    if segment not in SEGMENTS:
        logger.error(f"Unknown segment: {segment}")
        raise ValueError(f"Unknown segment: {segment}")
    if new_state not in EDP_STATES:
        logger.error(f"Unknown state: {new_state}")
        raise ValueError(f"Unknown state: {new_state}")
    # Update the state in the database or in-memory structure
    if prev_phase != new_state and new_state  in (SUCCEEDED, FAILED):
        send_alert(segment, new_state, remarks)
    update_segment_state(segment, new_state)
    logger.info(f"Segment {segment} moved to state {new_state}")

def get_segment_state_handler(segment: str, segment_state:str):
    if segment in ("CASH", "DR", "CUR", "SLBM", "MCX", "NCDEX", "MTF"):
        match segment_state:
            case "INIT":
                return handle_initializing_state
            case "WAITING_FOR_FILE_UPLOAD":
                return handle_waiting_for_file_upload_state
            case "WAITING_FOR_BILL_POSTING_COMPLETION":
                return handle_waiting_for_bill_posting_completion_state
            case "WAITING_FOR_RECON_COMPLETION":
                return handle_waiting_for_recon_completion_state
            case "WAITING_FOR_CNG_COMPLETION":
                return handle_waiting_for_cng_completion_state
            case "SUCCEEDED":
                return handle_succeeded_state
            case "FAILED":
                return handle_failed_state
            default:
                logger.error(f"Unknown state: {segment_state} for segment: {segment}")
                raise ValueError(f"Unknown state: {segment_state} for segment: {segment}")
    
    elif segment in ("COL_VAL"):
        match segment_state:
            case "WAITING_FOR_GTG":
                return handle_waiting_for_gtg_state
            case "SUCCEEDED":
                return handle_succeeded_state
            case "FAILED":
                return handle_failed_state
    else:
        logger.error(f"Unknown segment: {segment} or state: {segment_state}")
        raise ValueError(f"Unknown segment: {segment} or state: {segment_state}")
        
    # Similarly, initialize for other segments like DR, SLBM, etc.

async def state_machine_loop() -> None:
    try:
        current_time = time.time()
        atomic_loop_start_time = current_time
        for segment in SEGMENTS:
            if is_handled(segment):
                logger.info(f"Segment {segment} is already handled. Skipping.")
                continue
            st_time, end_time = get_segment_time_range(segment)
            if st_time <= current_time <= end_time:
                logger.info(f"Segment {segment} is in the time range. Processing.")
                segment_state = get_segment_state(segment)
                fn = get_segment_state_handler(segment, segment_state)
                try:
                    fn(segment,segment_state,current_time)
                except Exception as e:
                    logger.error(f"Error occurred while processing segment {segment} in function {fn.__name__}: {e}")
            elif current_time > st_time:
                move_to_state(segment, "FAILED", f"Segment {segment} in state {segment_state} has exceeded its processing time window. Marking as FAILED.",segment,segment_state)
            else:
                logger.info(f"Segment {segment} is not in the time range. Skipping.")
    except Exception as e:
        logger.error(f"Error occurred in state_machine_loop: {e}")
    
    finally:
        # schedule the next run of the state machine loop after a delay using asyncio
        asyncio.sleep(60)  # Wait for 60 seconds
        asyncio.create_task(state_machine_loop())  # Recursively call the loop to continue processing
        atomic_loop_end_time = time.time()

def is_handled(segment: str) -> bool:
    if segment not in SEGMENTS:
        logger.error(f"Unknown segment: {segment}")
        raise ValueError(f"Unknown segment: {segment}")
    return get_segment_state(segment) in (SegmentStatus.COMPLETED, SegmentStatus.SKIPPED)



def is_record_exists(segment: str) -> bool:
    if segment not in SEGMENTS:
        logger.error(f"Unknown segment: {segment}")
        raise ValueError(f"Unknown segment: {segment}")
    return get_segment_state(segment) in (SegmentStatus.COMPLETED, SegmentStatus.SKIPPED)

def handle_initializing_state(segment: str, segment_state: str, current_time: float) -> None:
    logger.info(f"Segment {segment} is in initializing state. Processing.")
    return

def handle_waiting_for_file_upload_state(segment: str, segment_state: str, current_time: float) -> None:
    logger.info(f"Segment {segment} is in waiting for file upload state. Processing.")
    return

def handle_waiting_for_bill_posting_completion_state(segment: str, segment_state: str, current_time: float) -> None:
    logger.info(f"Segment {segment} is in waiting for bill posting completion state. Processing.")
    return

def handle_waiting_for_recon_completion_state(segment: str, segment_state: str, current_time: float) -> None:
    logger.info(f"Segment {segment} is in waiting for recon completion state. Processing.")
    return

def handle_waiting_for_cng_completion_state(segment: str, segment_state: str, current_time: float) -> None:
    logger.info(f"Segment {segment} is in waiting for cng completion state. Processing.")
    return

def handle_succeeded_state(segment: str, segment_state: str, current_time: float) -> None:
    logger.info(f"Segment {segment} is in succeeded state. Processing.")
    return

