#------------------------------------------------------------------------------
# Liam Kelley's main Multimodal Mesh-Audio-Image HL2 recording script
#------------------------------------------------------------------------------

from pynput import keyboard
import multiprocessing as mp
import argparse
import glob
import os
import time

import hl2ss
import hl2ss_lnm
import hl2ss_mp
import hl2ss_utilities
import hl2ss_sa
import hl2ss_io
import lk_hl2ss

### Initial setup
# Get HoloLens IP
# Allow developper mode
# Download and install hl2ss from their github

### MESH RESET
# 1. Open settings > System > Holograms
# 2. Remove all holograms

### OPTIONAL 
# 1. Go to https://192.168.50.61
# 2. Check that the mesh is reset

#------------------------------------------------------------------------------
# Settings --------------------------------------------------------------------
#------------------------------------------------------------------------------

def parse_arguments():
    parser = argparse.ArgumentParser(description='lk multimodal dataset capture')

    # Add arguments
    parser.add_argument('--mute', type=bool, default=False, help='Flag to mute the audio (default: True)')
    parser.add_argument('--visualize', type=bool, default=False, help='Flag to visualize the data (default: False)')
    parser.add_argument('--roomname', type=str, default='LiamsOffice', help='Name of the room (default: LiamsOffice)')
    parser.add_argument('--host', type=str, default='192.168.50.61', help='Single HoloLens address (fallback if --hosts is not provided)')
    parser.add_argument('--hosts', nargs='+', default=None, help='List of HoloLens addresses for multi-device capture')
    parser.add_argument('--nrec', type=int, default=5, help='Number of consecutive recordings in a rec sesh (default: 5)')

    # Parse arguments
    return parser.parse_args()

# Recorded channels
channels = [
    "PERSONAL_VIDEO",
    "MICROPHONE",
    "SPATIAL_INPUT",
    "SPATIAL_MAPPING",
]

# PV parameters
pv_width     = 760
pv_height    = 428
pv_framerate = 30

# Spatial_Mapping manager parameters
tpcm = 500 # triangles per cubic meter
threads = 2
radius = 2

# # EET parameters (currently unused)
# eet_fps = 30 # 30, 60, 90 

def multi_rec_sesh_manager(overall_script_stop_event: mp.Event,
                           session_running_flag: mp.Event,
                           interrupt_session: mp.Event,
                           stop_audio_recording: mp.Event,
                           instruction_queues: dict[str, mp.Queue],
                           out_queues: dict[str, mp.Queue],
                           device_instruction_queues: dict[str, dict[str, mp.Queue]],
                           device_out_queues: dict[str, dict[str, mp.Queue]],
                           n_recordings: int,
                           room_name: str):
    '''
    Recording session manager that coordinates multiple HoloLens devices.
    '''
    out_queues["manager"].put("started")

    device_labels = list(device_instruction_queues.keys())
    if not device_labels:
        print("No devices configured. Stopping.")
        overall_script_stop_event.set()
        return

    room_root = room_name

    previous_sessions = glob.glob(os.path.join("dataset", room_root, f"session_*_{device_labels[0].lower()}"))
    if any(previous_sessions):
        session_no = max([
            int(os.path.basename(previous_session).split("_")[1])
            for previous_session in previous_sessions
        ])
        formatted_session_no = f"{session_no:03}"
    else:
        session_no = -1
        formatted_session_no = f"{session_no:03}"

    while not overall_script_stop_event.is_set():
        print("\nPlease check the following:")
        print(f"    1. Current room : {room_root}. (see --roomname argument)")
        print("    2. Did you properly get the source position for the previous session?")
        print("    3. Has the mesh been reset?")
        print("Waiting for instruction. Start = space, Stop = Esc, get previous src pov = L_shift\n")

        msg = instruction_queues["manager"].get()

        if msg == "start_rec_session":
            session_no += 1
            formatted_session_no = f"{session_no:03}"
            print(f"Starting new recording session n°{formatted_session_no}. n_recordings = {n_recordings}")

            session_running_flag.set()

            for i in range(5):
                print(f"{i} seconds to get set up...")
                time.sleep(1)

            i = 0
            while not interrupt_session.is_set() and i < n_recordings:
                i += 1
                print(f"Starting 10 seconds of exploration for all Holos...")
                for _ in range(10):
                    instruction_queues["audio_player"].put("countdown_beep")
                    assert (msg := out_queues["audio_player"].get()) == "countdown_beep_done", f"Expected 'countdown_beep_done', but got {msg}"
                time.sleep(1)

                timestamp = int(time.time())

                print(f"Starting recording n°{i} across {len(device_labels)} Holos...")
                instruction_queues["audio_player"].put("white_blast")
                assert (msg := out_queues["audio_player"].get()) == "white_blast_done", f"Expected 'white_blast_done', but got {msg}"

                stop_audio_recording.clear()

                for label in device_labels:
                    session_dir = f"session_{formatted_session_no}_{label.lower()}"
                    datapoint = os.path.join(room_root, session_dir, f"time_{timestamp}")
                    for key in ["mesh_recorder", "image_recorder"]:
                        device_instruction_queues[label][key].put("rec_start" + datapoint)

                for label in device_labels:
                    session_dir = f"session_{formatted_session_no}_{label.lower()}"
                    datapoint = os.path.join(room_root, session_dir, f"time_{timestamp}")
                    device_instruction_queues[label]["audio_recorder"].put("rec_start" + datapoint)

                for label in device_labels:
                    assert (msg := device_out_queues[label]["audio_recorder"].get()) == "receiver_opened", f"Expected 'receiver_opened' for {label}, but got {msg}"

                instruction_queues["audio_player"].put("ESS")
                assert (msg := out_queues["audio_player"].get()) == "ESS_done", f"Expected 'ESS_done', but got {msg}"
                time.sleep(0.3)

                stop_audio_recording.set()

                for label in device_labels:
                    for key in ["mesh_recorder", "image_recorder", "audio_recorder"]:
                        assert (msg := device_out_queues[label][key].get()) == "done", f"Expected 'done' for {label} ({key}), but got {msg}"

                print(f"Recording n°{i} is done for all Holos.")
                time.sleep(1)

            stop_audio_recording.clear()
            session_running_flag.clear()

            if interrupt_session.is_set():
                interrupt_session.clear()
                print("Recording session interrupted.")
            else:
                instruction_queues["manager"].put("get_src_pov")
                print("Recording session stopped. Moving on to getting src position.")

        elif msg == "get_src_pov":
            if session_no >= 0:
                print(f"Getting src pos for recording session n°{formatted_session_no}.")

                session_running_flag.set()
                time.sleep(0.5)

                print(f"Getting source pov for session {formatted_session_no}.")
                instruction_queues["audio_player"].put("src_pov_warning")
                assert (msg := out_queues["audio_player"].get()) == "src_pov_warning_done", f"Expected 'src_pov_warning_done', but got {msg}"

                for _ in range(5):
                    print("Waiting...")
                    time.sleep(1)
                print("Recording!")

                for label in device_labels:
                    session_dir = f"session_{formatted_session_no}_{label.lower()}"
                    datapoint = os.path.join(room_root, session_dir, "source_pov")
                    for key in ["mesh_recorder", "image_recorder"]:
                        device_instruction_queues[label][key].put("rec_start" + datapoint)

                instruction_queues["audio_player"].put("white_blast")
                assert (msg := out_queues["audio_player"].get()) == "white_blast_done", f"Expected 'white_blast_done', but got {msg}"
                time.sleep(0.5)

                stop_audio_recording.clear()
                for label in device_labels:
                    session_dir = f"session_{formatted_session_no}_{label.lower()}"
                    datapoint = os.path.join(room_root, session_dir, "source_pov")
                    device_instruction_queues[label]["audio_recorder"].put("rec_start" + datapoint)

                for label in device_labels:
                    assert (msg := device_out_queues[label]["audio_recorder"].get()) == "receiver_opened", f"Expected 'receiver_opened' for {label}, but got {msg}"

                instruction_queues["audio_player"].put("ESS")
                assert (msg := out_queues["audio_player"].get()) == "ESS_done", f"Expected 'ESS_done', but got {msg}"
                time.sleep(0.3)

                stop_audio_recording.set()

                for label in device_labels:
                    for key in ["mesh_recorder", "image_recorder", "audio_recorder"]:
                        assert (msg := device_out_queues[label][key].get()) == "done", f"Expected 'done' for {label} ({key}), but got {msg}"

                print(f"Got source pov for session {formatted_session_no}.")
                session_running_flag.clear()
            else:
                print("No sessions to get src pov for. Skipping.")

        elif msg == "stop":
            break
        else:
            print(f"Unexpected messsage in rec session manager instruction queue: {msg}")

    print("rec_session_manager_process stopped.")

if __name__ == '__main__':
    
    pargs = parse_arguments()
    hosts = pargs.hosts if pargs.hosts else [pargs.host]
    device_labels = [f"Holo{i+1}" for i in range(len(hosts))]
    if len(hosts) == 0:
        print("No HoloLens IPs provided. Please pass them via --hosts.")
        quit()
    
    print("\nWelcome to lk multimodal dataset capture !!")
    print(f"Configured Holos: {dict(zip(device_labels, hosts))}")
    
    #------------------------------------------------------------------------------
    # HL2 Setup -------------------------------------------------------------------
    #------------------------------------------------------------------------------
    
    if ("RM_DEPTH_LONGTHROW" in channels) and ("RM_DEPTH_AHAT" in channels):
        print('Error: Simultaneous RM Depth Long Throw and RM Depth AHAT streaming is not supported. See known issues at https://github.com/jdibenes/hl2ss.')
        quit()
    if ("SPATIAL_MAPPING" in channels) and not ("SPATIAL_INPUT" in channels):
        print('Error: Spatial Mapping requires Spatial Input.')
        quit()

    #------------------------------------------------------------------------------
    # Process setup ---------------------------------------------------------------
    #------------------------------------------------------------------------------
    
    overall_script_stop_event = mp.Event()
    session_running_flag = mp.Event()
    interrupt_session = mp.Event()
    stop_audio_recording = mp.Event()
    
    names = ["audio_player", "manager"]
    instruction_queues = {name: mp.Queue() for name in names}
    out_queues = {name: mp.Queue() for name in names}
    device_instruction_queues: dict[str, dict[str, mp.Queue]] = {}
    device_out_queues: dict[str, dict[str, mp.Queue]] = {}
    
    processes = []
    
    audio_player_process = mp.Process(target=lk_hl2ss.audio_player,
                                        args=(overall_script_stop_event,
                                            instruction_queues["audio_player"],
                                            out_queues["audio_player"],
                                            pargs.mute))
    processes.append(audio_player_process)
    
    for idx, host in enumerate(hosts):
        label = device_labels[idx]
        device_instruction_queues[label] = {name: mp.Queue() for name in ["audio_recorder", "mesh_recorder", "image_recorder"]}
        device_out_queues[label] = {name: mp.Queue() for name in ["audio_recorder", "mesh_recorder", "image_recorder"]}

        receivers = {
            "RM_VLC_LEFTFRONT" : hl2ss_lnm.rx_rm_vlc(host, hl2ss.StreamPort.RM_VLC_LEFTFRONT, decoded=True),
            "RM_VLC_LEFTLEFT" : hl2ss_lnm.rx_rm_vlc(host, hl2ss.StreamPort.RM_VLC_LEFTLEFT, decoded=True),
            "RM_VLC_RIGHTFRONT" : hl2ss_lnm.rx_rm_vlc(host, hl2ss.StreamPort.RM_VLC_RIGHTFRONT, decoded=True),
            "RM_VLC_RIGHTRIGHT" : hl2ss_lnm.rx_rm_vlc(host, hl2ss.StreamPort.RM_VLC_RIGHTRIGHT, decoded=True),
            # "RM_DEPTH_AHAT" : hl2ss_lnm.rx_rm_depth_ahat(host, hl2ss.StreamPort.RM_DEPTH_AHAT, decoded=True),
            "RM_DEPTH_LONGTHROW" : hl2ss_lnm.rx_rm_depth_longthrow(host, hl2ss.StreamPort.RM_DEPTH_LONGTHROW, decoded=True),
            # "RM_IMU_ACCELEROMETER" : hl2ss_lnm.rx_rm_imu(host, hl2ss.StreamPort.RM_IMU_ACCELEROMETER),
            # "RM_IMU_GYROSCOPE" : hl2ss_lnm.rx_rm_imu(host, hl2ss.StreamPort.RM_IMU_GYROSCOPE),
            # "RM_IMU_MAGNETOMETER" : hl2ss_lnm.rx_rm_imu(host, hl2ss.StreamPort.RM_IMU_MAGNETOMETER),
            "PERSONAL_VIDEO" : hl2ss_lnm.rx_pv(host, hl2ss.StreamPort.PERSONAL_VIDEO, width=pv_width, height=pv_height,
                                                framerate=pv_framerate, decoded_format = 'rgb24', mode = hl2ss.StreamMode.MODE_1), # Stream mode 1 includes camera pose, 0 doesn't
            "MICROPHONE" : hl2ss_lnm.rx_microphone(host, hl2ss.StreamPort.MICROPHONE, profile=hl2ss.AudioProfile.RAW, level=hl2ss.AACLevel.L5), # Miicrophone Array
            "SPATIAL_INPUT" : hl2ss_lnm.rx_si(host, hl2ss.StreamPort.SPATIAL_INPUT),
            # "EXTENDED_EYE_TRACKER" : hl2ss_lnm.rx_eet(host, hl2ss.StreamPort.EXTENDED_EYE_TRACKER, fps=eet_fps),
            # "EXTENDED_AUDIO" : hl2ss_lnm.rx_extended_audio(host, hl2ss.StreamPort.EXTENDED_AUDIO, decoded=False),
        }
        
        if ("PERSONAL_VIDEO" in channels):
            hl2ss_lnm.start_subsystem_pv(host, hl2ss.StreamPort.PERSONAL_VIDEO)
        
        sm_manager = None
        if "SPATIAL_MAPPING" in channels:
            sm_manager = hl2ss_sa.sm_manager(host, tpcm, threads)
        
        mesh_recorder_process = mp.Process(target=lk_hl2ss.mesh_recorder,
                                            args=(overall_script_stop_event,
                                                  stop_audio_recording,
                                                device_instruction_queues[label]["mesh_recorder"],
                                                device_out_queues[label]["mesh_recorder"],
                                                receivers["SPATIAL_INPUT"],
                                                sm_manager,
                                                radius,
                                                pargs.visualize))
        audio_recorder_process = mp.Process(target=lk_hl2ss.audio_recorder,
                                            args=(overall_script_stop_event,
                                                stop_audio_recording,
                                                device_instruction_queues[label]["audio_recorder"],
                                                device_out_queues[label]["audio_recorder"],
                                                receivers["MICROPHONE"],
                                                pargs.visualize))
        
        img_receivers = {
            "PERSONAL_VIDEO" : receivers["PERSONAL_VIDEO"],
            "RM_VLC_LEFTFRONT" : receivers["RM_VLC_LEFTFRONT"],
            "RM_VLC_LEFTLEFT" : receivers["RM_VLC_LEFTLEFT"],
            "RM_VLC_RIGHTFRONT" : receivers["RM_VLC_RIGHTFRONT"],
            "RM_VLC_RIGHTRIGHT" : receivers["RM_VLC_RIGHTRIGHT"],
            "RM_DEPTH_LONGTHROW" : receivers["RM_DEPTH_LONGTHROW"],
        }
        image_recorder_process = mp.Process(target=lk_hl2ss.image_recorder,
                                            args=(overall_script_stop_event,
                                                device_instruction_queues[label]["image_recorder"],
                                                device_out_queues[label]["image_recorder"],
                                                img_receivers,
                                                pargs.visualize))

        processes.extend([mesh_recorder_process, audio_recorder_process, image_recorder_process])

    rec_session_manager_process = mp.Process(target=multi_rec_sesh_manager,
                                    args=(overall_script_stop_event,
                                        session_running_flag,
                                        interrupt_session,
                                        stop_audio_recording,
                                        instruction_queues,
                                        out_queues,
                                        device_instruction_queues,
                                        device_out_queues,
                                        pargs.nrec,
                                        pargs.roomname))
    processes.append(rec_session_manager_process)
    
    for process in processes:
        process.start()

    for key, queue in out_queues.items():
        assert (msg := queue.get()) == "started", f"Expected 'started' for {key}, but got {msg}"
    for label, queue_dict in device_out_queues.items():
        for key, queue in queue_dict.items():
            assert (msg := queue.get()) == "started", f"Expected 'started' for {label} ({key}), but got {msg}"

    #------------------------------------------------------------------------------
    # Start / stop  via Keyboard Manager ------------------------------------------
    #------------------------------------------------------------------------------

    def on_press(key):
        '''
        This function runs when a key is pressed on the keyboard.
        When space is pressed, and no process is yet created, a new process (and stop event) is created.
        This process is to manage the recording.
        
        When esc is pressed, and there is an active process, the stop event is set.
        The process will hear this, and should stop itself cleanly.
        
        The keys can then be pressed again to start new recordings.
        If there are no processes, esc can be used to quit the recording manager.
        '''
        global overall_script_stop_event
        global session_running_flag
        global interrupt_session
        global instruction_queues
        global device_instruction_queues

        if not overall_script_stop_event.is_set():
            if key == keyboard.Key.space:
                print("Signaling to manager to start recording session")
                while not instruction_queues["manager"].empty():
                    instruction_queues["manager"].get()
                instruction_queues["manager"].put("start_rec_session")
            
            elif key == keyboard.Key.esc:
                if session_running_flag.is_set():
                    print("Session underway. Signaling to manager to stop recording session")
                    interrupt_session.set()
                    while not instruction_queues["manager"].empty():
                        instruction_queues["manager"].get()
                else:
                    print("Signaling to all to stop")
                    for queue in instruction_queues.values():
                        queue.put("stop")
                    for queue_dict in device_instruction_queues.values():
                        for queue in queue_dict.values():
                            queue.put("stop")
                    overall_script_stop_event.set()
            
            elif key == keyboard.Key.shift_l:
                if session_running_flag.is_set():
                    print("Session underway. Signaling to manager to move on to getting source position")
                    interrupt_session.set()
                    while not instruction_queues["manager"].empty():
                        instruction_queues["manager"].get()
                    instruction_queues["manager"].put("get_src_pov")
                else:
                    print("Signaling to manager to get the source position for the previous session")
                    while not instruction_queues["manager"].empty():
                        instruction_queues["manager"].get()
                    instruction_queues["manager"].put("get_src_pov")
                    
                    
                    
                

    listener = keyboard.Listener(on_press=on_press)
    listener.start()
    
    #------------------------------------------------------------------------------
    # Run & Stop & Cleanup --------------------------------------------------------
    #------------------------------------------------------------------------------

    overall_script_stop_event.wait()

    listener.stop()
    listener.join()
    print("Keyboard Listener stopped.")
    
    for process in processes:
        process.join()
    print("All processes stopped. Cheers!")
    
