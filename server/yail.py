#!/usr/bin/env python

import os
import argparse
from typing import List, Union, Callable
import requests
import re
import time
import logging
from tqdm import tqdm
import socket
import threading
from threading import Thread, Lock
import random
from fastcore.all import *
from pprint import pformat
from PIL import Image
import numpy as np
import sys
from dotenv import load_dotenv
from io import BytesIO
import base64
import signal
import threading
import traceback

# Import image generation functionality from yail_gen module
from yail_gen import (
    ImageGenConfig, 
    generate_image, 
    generate_image_with_openai, 
    generate_image_with_gemini,
    gen_config,
    initialize_gen_config,
    OPENAI_AVAILABLE,
    GEMINI_AVAILABLE
)

# Import camera functionality from yail_camera module
from yail_camera import (
    init_camera,
    capture_camera_image,
    shutdown_camera,
    PYGAME_AVAILABLE
)

# Set up logging first thing
logging.basicConfig(level=logging.INFO, 
                    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Constants for image processing
SOCKET_WAIT_TIME = 1
GRAPHICS_8 = 2
GRAPHICS_9 = 4
GRAPHICS_11 = 8
VBXE = 16
# These need to be generalized.  The server/client protocol should specify this.
YAIL_W = 320
YAIL_H = 220
VBXE_W = 640
VBXE_H = 480

DL_BLOCK = 0x04
XDL_BLOCK = 0x05
PALETTE_BLOCK = 0x06
IMAGE_BLOCK = 0x07
ERROR_BLOCK = 0xFF

# Global variables
yail_data = bytearray()
yail_mutex = threading.Lock()
active_client_threads = []  # Track active client threads
connections = 0
filenames = []
last_prompt = None
last_gen_model = None

def prep_image_for_vbxe(image: Image.Image, target_width: int=YAIL_W, target_height: int=YAIL_H) -> Image.Image:
    logger.info(f'Image size: {image.size}')

    # Calculate the new size preserving the aspect ratio
    image_ratio = image.width / image.height
    target_ratio = target_width / target_height

    if image_ratio > target_ratio:
        # Image is wider than target, fit to width
        new_width = target_width
        new_height = int(target_width / image_ratio)
    else:
        # Image is taller than target, fit to height
        new_width = int(target_height * image_ratio)
        new_height = target_height

    # Resize the image
    image = image.resize((new_width, new_height), Image.BILINEAR)
    logger.info(f'Image new size: {image.size}')

    # Create a new image with the target size and a black background
    new_image = Image.new('RGB', (target_width, target_height), (0, 0, 0))

    # Calculate the position to paste the resized image onto the black background
    paste_x = (target_width - image.width) // 2
    paste_y = (target_height - image.height) // 2

    # Paste the resized image onto the black background
    new_image.paste(image, (paste_x, paste_y))

    # Replace the original image with the new image
    return new_image


def fix_aspect(image: Image.Image, crop: bool=False) -> Image.Image:
    aspect = YAIL_W/YAIL_H   # YAIL aspect ratio
    aspect_i = 1/aspect
    w = image.size[0]
    h = image.size[1]
    img_aspect = w/h

    if crop:
        if img_aspect > aspect:  # wider than YAIL aspect
            new_width = int(h * aspect)
            new_width_diff = w - new_width
            new_width_diff_half = int(new_width_diff/2)
            image = image.crop((new_width_diff_half, 0, w-new_width_diff_half, h))
        else:                    # taller than YAIL aspect
            new_height = int(w * aspect_i)
            new_height_diff = h - new_height
            new_height_diff_half = int(new_height_diff/2)
            image = image.crop((0, new_height_diff_half, w, h-new_height_diff_half))
    else:
        if img_aspect > aspect:  # wider than YAIL aspect
            new_height = int(w * aspect_i)
            background = Image.new("L", (w,new_height))
            background.paste(image, (0, int((new_height-h)/2)))
            image = background
        else:                    # taller than YAIL aspect
            new_width = int(h * aspect)
            background = Image.new("L", (new_width, h))
            background.paste(image, (int((new_width-w)/2), 0))
            image = background

    return image

def dither_image(image: Image.Image) -> Image.Image:
    return image.convert('1')

def pack_bits(image: Image.Image) -> np.ndarray:
    bits = np.array(image)
    return np.packbits(bits, axis=1)

def pack_shades(image: Image.Image) -> np.ndarray:
    yail = image.resize((int(YAIL_W/4),YAIL_H), Image.LANCZOS)
    yail = yail.convert(dither=Image.FLOYDSTEINBERG, colors=16)

    im_matrix = np.array(yail)
    im_values = im_matrix[:,:]

    evens = im_values[:,::2]
    odds = im_values[:,1::2]

    # Each byte holds 2 pixels.  The upper four bits for the left pixel and the lower four bits for the right pixel.
    evens_scaled = (evens >> 4) << 4 # left pixel
    odds_scaled =  (odds >> 4)       # right pixel

    # Combine the two 4bit values into a single byte
    combined = evens_scaled + odds_scaled
    
    return combined.astype('int8')

def show_dithered(image: Image.Image) -> None:
    image.show()

def show_shades(image_data: np.ndarray) -> None:
    pil_image_yai = Image.fromarray(image_data, mode='L')
    pil_image_yai.resize((320,220), resample=None).show()

def convertToYai(image_data: bytearray, gfx_mode: int) -> bytearray:
    import struct

    ttlbytes = image_data.shape[0] * image_data.shape[1]

    image_yai = bytearray()
    image_yai += bytes([1, 1, 0])            # version
    image_yai += bytes([gfx_mode])           # Gfx mode (8,9)
    image_yai += bytes([3])                  # Memory block type
    image_yai += struct.pack("<H", ttlbytes) # num bytes height x width
    image_yai += bytearray(image_data)       # image

    logger.debug(f'YAI size: {len(image_yai)}')

    return image_yai

def createErrorPacket(error_message: str, gfx_mode: int) -> bytearray:
    import struct

    logger.debug(f'Error message length: {len(error_message)}')

    error_packets = bytearray()
    error_packets += bytes([1, 4, 0])                      # version
    error_packets += bytes([gfx_mode])                     # Gfx mode (8,9)
    error_packets += struct.pack("<B", 1)                  # number of memory blocks
    error_packets += bytes([ERROR_BLOCK])                  # Memory block type
    error_packets += struct.pack("<I", len(error_message)) # error message size
    error_packets += bytearray(error_message)              # error

    return error_packets


def convertToYaiVBXE(image_data: bytes, palette_data: bytes, gfx_mode: int) -> bytearray:
    import struct

    # Log information about the source image
    logger.debug(f'Image data size: {len(image_data)}')
    logger.debug(f'Palette data size: {len(palette_data)}')

    image_yai = bytearray()
    image_yai += bytes([1, 4, 0])            # version
    image_yai += bytes([gfx_mode])           # Gfx mode (8,9)
    image_yai += struct.pack("<B", 2)        # number of memory blocks
    image_yai += bytes([PALETTE_BLOCK])             # Memory block type
    image_yai += struct.pack("<I", len(palette_data)) # palette size
    image_yai += bytearray(palette_data)  # palette
    image_yai += bytes([IMAGE_BLOCK])                  # Memory block type
    image_yai += struct.pack("<I", len(image_data)) # num bytes height x width
    image_yai += bytearray(image_data)       # image

    logger.debug(f'YAI size: {len(image_yai)}')

    return image_yai

def convertImageToYAIL(image: Image.Image, gfx_mode: int) -> bytearray:
    # Log information about the source image
    logger.debug(f'Source Image size: {image.size}')
    logger.debug(f'Source Image mode: {image.mode}')
    logger.debug(f'Source Image format: {image.format}')
    logger.debug(f'Source Image info: {image.info}')

    if gfx_mode == GRAPHICS_8 or gfx_mode == GRAPHICS_9:
        gray = image.convert(mode='L')
        gray = fix_aspect(gray)
        gray = gray.resize((YAIL_W,YAIL_H), Image.LANCZOS)

        logger.debug(f'Processed Image size: {image.size}')
        logger.debug(f'Processed Image mode: {image.mode}')
        logger.debug(f'Processed Image format: {image.format}')
        logger.debug(f'Processed Image info: {image.info}')

        if gfx_mode == GRAPHICS_8:
            gray_dithered = dither_image(gray)
            image_data = pack_bits(gray_dithered)
        elif gfx_mode == GRAPHICS_9:
            image_data = pack_shades(gray)

        image_yai = convertToYai(image_data, gfx_mode)

    else: # gfx_mode == VBXE:
        # Make the image fit out screen format but preserve it's aspect ratio
        image_resized = prep_image_for_vbxe(image, target_width=320, target_height=240)
        # Convert the image to use a palette
        image_resized = image_resized.convert('P', palette=Image.ADAPTIVE, colors=256)
        logger.info(f'Image size: {image_resized.size}')
        #image_resized.show()
        # Get the palette
        palette = image_resized.getpalette()
        # Get the image data
        image_resized = image_resized.tobytes()
        logger.info(f'Image data size: {len(image_resized)}')
        # Offset the palette entries by one
        offset_palette = [0] * 3 + palette[:-3]
        # Offset the image data by one
        offset_image_data = bytes((byte + 1) % 256 for byte in image_resized)

        image_yai = convertToYaiVBXE(offset_image_data, offset_palette, gfx_mode)

    return image_yai

def update_yail_data(data: np.ndarray, gfx_mode: int, thread_safe: bool = True) -> None:
    global yail_data
    if thread_safe:
        yail_mutex.acquire()
    try:
        yail_data = data #convertToYai(data, gfx_mode)
    finally:
        if thread_safe:
            yail_mutex.release()

def send_yail_data(client_socket: socket.socket, thread_safe: bool=True) -> None:
    global yail_data

    if thread_safe:
        yail_mutex.acquire()
    try:
        data = yail_data   # a local copy
    finally:
        if thread_safe:
            yail_mutex.release()

    if data is not None:
        client_socket.sendall(data)
        logger.info('Sent YAIL data')

def stream_YAI(client: str, gfx_mode: int, url: str = None, filepath: str = None) -> bool:
    from io import BytesIO

    global YAIL_H

    # download the body of response by chunk, not immediately
    try:
        if url is not None:
            logger.info(f'Loading {url}')

            file_size = 0

            response = requests.get(url, stream=True, timeout=5)

            # get the file name
            filepath = ''
            exts = ['.jpg', '.jpeg', '.gif', '.png']
            ext = re.findall('|'.join(exts), url)
            if len(ext):
                pos_ext = url.find(ext[0])
                if pos_ext >= 0:
                    pos_name = url.rfind("/", 0, pos_ext)
                    filepath =  url[pos_name+1:pos_ext+4]

            # progress bar, changing the unit to bytes instead of iteration (default by tqdm)
            image_data = b''
            progress = tqdm(response.iter_content(256), f"Downloading {filepath}", total=file_size, unit="B", unit_scale=True, unit_divisor=256)
            for data in progress:
                # collect all the data
                image_data += data

                # update the progress bar manually
                progress.update(len(data))

            image_bytes_io = BytesIO()
            image_bytes_io.write(image_data)
            image = Image.open(image_bytes_io)

        elif filepath is not None:
            image = Image.open(filepath)

        image_yai = convertImageToYAIL(image, gfx_mode)

        client.sendall(image_yai)

        return True

    except Exception as e:
        logger.error(f'Exception: {e} **{file_size}')
        return False

# This uses the DuckDuckGo search engine to find images.  This is handled by the duckduckgo_search package.
def search_images(term: str, max_images: int=1000) -> List[str]:
    """
    Search for images using DuckDuckGo and return a list of URLs.
    
    Args:
        term (str): The search term
        max_images (int): Maximum number of images to return
        
    Returns:
        List[str]: List of image URLs
    """
    try:
        from ddgs import DDGS
        results = DDGS().images(query=term, max_results=max_images)
        
        # Extract image URLs from results
        urls = [result['image'] for result in results]
        logger.info(f"Found {len(urls)} images for search term: '{term}'")
        return urls
    except Exception as e:
        logger.error(f"Error searching for images '{term}': {e}")
        return []

def stream_random_image_from_urls(client_socket: socket.socket, urls: list, gfx_mode: int) -> None:
    """
    Stream a random image from a list of URLs to the client.
    Handles retries if an image fails to stream.
    
    Args:
        client_socket: The client socket to stream to
        urls: List of image URLs
        gfx_mode: The graphics mode to use
    """
    if not urls:
        send_client_response(client_socket, "No images found", is_error=True)
        return
        
    url_idx = random.randint(0, len(urls)-1)
    url = urls[url_idx]
    
    # Loop if we have a problem with the image, selecting the next
    while not stream_YAI(client_socket, gfx_mode, url=url):
        logger.warning(f'Problem with {url} trying another...')
        url_idx = random.randint(0, len(urls)-1)
        url = urls[url_idx]
        time.sleep(SOCKET_WAIT_TIME)  # Give some breathing room.  Sleep for a second

def stream_random_image_from_files(client_socket: socket.socket, gfx_mode: int) -> None:
    """
    Stream a random image from the loaded filenames to the client.
    Handles retries if an image fails to stream.
    
    Args:
        client_socket: The client socket to stream to
        gfx_mode: The graphics mode to use
    """
    if not filenames:
        send_client_response(client_socket, "No image files available", is_error=True)
        return
        
    file_idx = random.randint(0, len(filenames)-1)
    filename = filenames[file_idx]
    
    # Loop if we have a problem with the image, selecting the next
    while not stream_YAI(client_socket, gfx_mode, filepath=filename):
        logger.warning(f'Problem with {filename} trying another...')
        file_idx = random.randint(0, len(filenames)-1)
        filename = filenames[file_idx]
        time.sleep(SOCKET_WAIT_TIME)

def send_client_response(client_socket: socket.socket, message: str, is_error: bool = False) -> None:
    """
    Send a standardized response to the client.
    
    Args:
        client_socket: The client socket to send the response to
        message: The message to send
        is_error: Whether this is an error message
    """
    prefix = "ERROR: " if is_error else "OK: "
    try:
        if is_error:
            message_packet = createErrorPacket(message.encode('utf-8'), gfx_mode=GRAPHICS_8)
            client_socket.sendall(message_packet)
        else:
            # For non-error messages, send as plain text with OK prefix
            client_socket.sendall(bytes(f"{prefix}{message}\r\n".encode('utf-8')))
            
        if is_error:
            logger.warning(f"Sent error to client: {message}")
        else:
            logger.info(f"Sent response to client: {message}")
    except Exception as e:
        logger.error(f"Failed to send response to client: {e}")

def stream_generated_image(client_socket: socket.socket, prompt: str, gfx_mode: int) -> None:
    """
    Generate an image with the configured model and stream it to the client.
    
    Args:
        client_socket: The client socket to stream to
        prompt: The text prompt for image generation
        gfx_mode: The graphics mode to use
    """
    logger.info(f"Generating image with prompt: '{prompt}'")
    
    # Generate image using the configured model
    url_or_path = generate_image(prompt)
    
    if url_or_path:
        # Stream the generated image to the client
        if url_or_path.startswith('http'):
            # It's a URL (from OpenAI)
            if not stream_YAI(client_socket, gfx_mode, url=url_or_path):
                logger.warning(f'Problem with generated image: {url_or_path}')
                send_client_response(client_socket, "Failed to stream generated image", is_error=True)
        else:
            # It's a local file path (from Gemini)
            if not stream_YAI(client_socket, gfx_mode, filepath=url_or_path):
                logger.warning(f'Problem with generated image: {url_or_path}')
                send_client_response(client_socket, "Failed to stream generated image", is_error=True)
    else:
        logger.warning('Failed to generate image')
        send_client_response(client_socket, "Failed to generate image", is_error=True)

def stream_generated_image_gemini(client_socket: socket.socket, prompt: str, gfx_mode: int) -> None:
    """
    Generate an image with Gemini and stream it to the client.
    
    Args:
        client_socket: The client socket to stream to
        prompt: The text prompt for image generation
        gfx_mode: The graphics mode to use
    """
    logger.info(f"Generating image with prompt: '{prompt}'")
    
    # Generate image using Gemini
    image_path = generate_image_with_gemini(prompt)
    
    if image_path:
        # Stream the generated image to the client
        if not stream_YAI(client_socket, gfx_mode, filepath=image_path):
            logger.warning(f'Problem with generated image: {image_path}')
            send_client_response(client_socket, "Failed to stream generated image", is_error=True)
    else:
        logger.warning('Failed to generate image with Gemini')
        send_client_response(client_socket, "Failed to generate image", is_error=True)

def handle_client_connection(client_socket: socket.socket, thread_id: int) -> None:
    """
    Handle a client connection in a separate thread.
    
    Args:
        client_socket: The client socket to handle
        thread_id: The ID of this client thread for tracking
    """
    global active_client_threads
    global last_prompt
    global last_gen_model
    global gen_config
    global connections
    
    logger.info(f"Starting Connection: {thread_id}")
    
    connections += 1
    logger.info(f'Starting Connection: {connections}')
    
    gfx_mode = GRAPHICS_8
    client_mode = None
    last_prompt = None  # Store the last prompt for regeneration

    try:
        client_socket.settimeout(300)  # 5 minutes timeout
        done = False
        urls = []
        url_idx = 0
        tokens = []
        while not done:
            if len(tokens) == 0:
                request = client_socket.recv(1024)
                logger.info(f'{thread_id} Client request {request}')
                
                # Check if this looks like an HTTP request
                if request.startswith(b'GET') or request.startswith(b'POST') or request.startswith(b'PUT') or request.startswith(b'DELETE') or request.startswith(b'HEAD'):
                    logger.warning("HTTP request detected - sending 'Not Allowed' response")
                    http_response = "HTTP/1.1 403 Forbidden\r\nContent-Type: text/plain\r\nContent-Length: 11\r\n\r\nNot Allowed"
                    client_socket.sendall(http_response.encode('utf-8'))
                    break
                
                r_string = request.decode('UTF-8')
                tokens = r_string.rstrip(' \r\n').split(' ')
            logger.info(f'{thread_id} Tokens {tokens}')

            if tokens[0] == 'video':
                client_mode = 'video'
                # Send a single frame from the camera to trigger the "next" response
                vid_frame = capture_camera_image(YAIL_W, YAIL_H)
                vid_frame_yail = convertImageToYAIL(vid_frame, gfx_mode)
                client_socket.sendall(vid_frame_yail)
                tokens.pop(0)

            elif tokens[0] == 'search':
                client_mode = 'search'
                # Join all tokens after 'search' as the search term
                prompt = ' '.join(tokens[1:])
                logger.info(f"Received search {prompt}")
                urls = search_images(prompt)
                stream_random_image_from_urls(client_socket, urls, gfx_mode)
                tokens = []

            elif tokens[0][:3] == 'gen':
                client_mode = 'generate'
                # Join all tokens after 'generate' as the prompt
                ai_model_name = tokens[1]
                prompt = ' '.join(tokens[2:])
                logger.info(f"{thread_id} Received {tokens[0]} model={ai_model_name} prompt={prompt}")
                last_prompt = prompt  # Store the prompt for later use with 'next' command
                stream_generated_image(client_socket, prompt, gfx_mode)
                tokens = []

            elif tokens[0] == 'files':
                client_mode = 'files'
                stream_random_image_from_files(client_socket, gfx_mode)
                tokens.pop(0)

            elif tokens[0] == 'next':
                if client_mode == 'search':
                    stream_random_image_from_urls(client_socket, urls, gfx_mode)
                    tokens.pop(0)
                elif client_mode == 'video':
                    vid_frame = capture_camera_image(YAIL_W, YAIL_H)
                    vid_frame_yail = convertImageToYAIL(vid_frame, gfx_mode)
                    client_socket.sendall(vid_frame_yail)
                    #send_yail_data(client_socket)
                    tokens.pop(0)
                elif client_mode == 'generate':
                    # For generate mode, we'll regenerate with the same prompt
                    # The prompt is stored in last_prompt
                    prompt = last_prompt
                    logger.info(f"{thread_id} Regenerating image with prompt: '{prompt}'")
                    stream_generated_image(client_socket, prompt, gfx_mode)
                    tokens.pop(0)
                elif client_mode == 'files':
                    stream_random_image_from_files(client_socket, gfx_mode)
                    tokens.pop(0)
                else:
                    send_client_response(client_socket, "No previous command to repeat", is_error=True)

            elif tokens[0] == 'gfx':
                tokens.pop(0)
                gfx_mode = int(tokens[0])
                #if gfx_mode > GRAPHICS_9:  # VBXE
                #    global YAIL_H
                #    YAIL_H = 240
                tokens.pop(0)

            elif tokens[0] == 'openai-config':
                tokens.pop(0)
                if len(tokens) > 0:
                    # Process OpenAI configuration parameters
                    
                    # Format: openai-config [param] [value]
                    param = tokens[0].lower()
                    tokens.pop(0)
                    
                    if len(tokens) > 0:
                        value = tokens[0]
                        tokens.pop(0)
                        
                        if param == "model":
                            if gen_config.set_model(value):
                                send_client_response(client_socket, f"OpenAI model set to {value}")
                            else:
                                send_client_response(client_socket, "Invalid model. Use 'dall-e-3' or 'dall-e-2'", is_error=True)
                        
                        elif param == "size":
                            if gen_config.set_size(value):
                                send_client_response(client_socket, f"Image size set to {value}")
                            else:
                                send_client_response(client_socket, "Invalid size. Use '1024x1024', '1792x1024', or '1024x1792'", is_error=True)
                        
                        elif param == "quality":
                            if gen_config.set_quality(value):
                                send_client_response(client_socket, f"Image quality set to {value}")
                            else:
                                send_client_response(client_socket, "Invalid quality. Use 'standard' or 'hd'", is_error=True)
                        
                        elif param == "style":
                            if gen_config.set_style(value):
                                send_client_response(client_socket, f"Image style set to {value}")
                            else:
                                send_client_response(client_socket, "Invalid style. Use 'vivid' or 'natural'", is_error=True)
                        
                        elif param == "system_prompt":
                            if gen_config.set_system_prompt(value):
                                send_client_response(client_socket, f"System prompt set to {value}")
                            else:
                                send_client_response(client_socket, "Failed to set system prompt", is_error=True)
                        
                        else:
                            send_client_response(client_socket, f"Unknown parameter '{param}'. Use 'model', 'size', 'quality', 'style', or 'system_prompt'", is_error=True)
                    else:
                        send_client_response(client_socket, f"Current OpenAI config: {gen_config}")
                else:
                    send_client_response(client_socket, f"Current OpenAI config: {gen_config}")

            elif tokens[0] == 'gen':
                client_mode = 'generate'
                # Join all tokens after 'gen' as the prompt
                prompt = ' '.join(tokens[1:])
                logger.info(f"{thread_id} Received gen {prompt}")
                last_prompt = prompt  # Store the prompt for later use with 'next' command
                stream_generated_image(client_socket, prompt, gfx_mode)
                tokens = []

            elif tokens[0] == 'gen-gemini':
                client_mode = 'generate'
                # Join all tokens after 'gen-gemini' as the prompt
                prompt = ' '.join(tokens[1:])
                logger.info(f"{thread_id} Received gen-gemini {prompt}")
                last_prompt = prompt  # Store the prompt for later use with 'next' command
                stream_generated_image_gemini(client_socket, prompt, gfx_mode)
                tokens = []

            elif tokens[0] == 'quit':
                done = True
                tokens.pop(0)

            else:
                tokens = [] # reset tokens if unrecognized command
                r_string = r_string.rstrip(" \r\n")   # strip whitespace
                logger.info(f'{thread_id} Received {r_string}')
                send_client_response(client_socket, "ACK!")

    except socket.timeout:
        logger.warning(f"Client connection {thread_id} timed out")
    except ConnectionResetError:
        logger.warning(f"Client connection {thread_id} was reset by the client")
    except BrokenPipeError:
        logger.warning(f"Client connection {thread_id} has a broken pipe")
    except Exception as e:
        logger.error(f"Error handling client connection {thread_id}: {e}")
        logger.error(traceback.format_exc())
    finally:
        # Clean up resources
        try:
            client_socket.close()
            logger.info(f"Closing Connection: {thread_id}")
            
            # Update connection counter
            connections -= 1
            logger.info(f"Active connections: {connections}")

        except Exception as e:
            logger.error(f"Error closing client socket for connection {thread_id}: {e}")

    logger.debug(f"handle_client_connection thread exiting: {threading.get_native_id()}")

def process_files(input_path: Union[str, List[str]], 
                  extensions: List[str], 
                  F: Callable[[str], None]) -> None:
    extensions = [ext.lower() if ext.startswith('.') else f'.{ext.lower()}' for ext in extensions]

    def process_file(file_path: str):
        _, ext = os.path.splitext(file_path)
        if ext.lower() in extensions:
            F(file_path)

    if isinstance(input_path, list):
        for file_path in input_path:
            process_file(file_path)
    elif os.path.isdir(input_path):
        for root, _, files in os.walk(input_path):
            for file in files:
                process_file(os.path.join(root, file))
    else:
        raise ValueError("input_path must be a directory path or a list of file paths.")

def F(file_path):
    global filenames
    logger.info(f"Processing file: {file_path}")
    filenames.append(file_path)

def main():
    """
    Main function to start the YAIL server.
    """
    global active_client_threads
    global gen_config
    
    # Track active client threads
    active_threads = []
    
    # Signal handler for graceful shutdown
    def signal_handler(sig, frame):
        logger.info("Shutting down YAIL server...")
        
        # Stop the camera thread if it's running
        shutdown_camera()
        
        # Close the server socket
        if 'server' in locals():
            try:
                server.close()
                logger.info("Server socket closed")
            except Exception as e:
                logger.error(f"Error closing server socket: {e}")
        
        # Wait for all client threads to finish (with timeout)
        if active_threads:
            logger.info(f"Waiting for {len(active_threads)} client threads to finish...")
            for thread in active_threads:
                if thread.is_alive():
                    thread.join(timeout=1.0)  # Wait up to 1 second for each thread
        
        logger.info("YAIL server shutdown complete")
        sys.exit(0)
    
    # Register signal handlers
    signal.signal(signal.SIGINT, signal_handler)   # Ctrl+C
    signal.signal(signal.SIGTERM, signal_handler)  # Termination signal
    
    # Initialize the image object to send with something
    initial_image = convertImageToYAIL(Image.new("L", (YAIL_W,YAIL_H)), GRAPHICS_8)
    update_yail_data(initial_image, GRAPHICS_8) #pack_shades(initial_image), GRAPHICS_8)

    bind_ip = '0.0.0.0'
    bind_port = 5556

    # Parse command line arguments
    parser = argparse.ArgumentParser(description='YAIL Server')
    parser.add_argument('--paths', nargs='*', help='Directory containing images to stream')
    parser.add_argument('--extensions', nargs='*', default=['.jpg', '.jpeg', '.gif', '.png'], help='File extensions to include')
    parser.add_argument('--camera', nargs='?', help='Camera device to use')
    parser.add_argument('--port', nargs=1, help='Port to listen on')
    parser.add_argument('--loglevel', nargs=1, help='Logging level')
    parser.add_argument('--openai-api-key', nargs=1, help='OpenAI API key')
    parser.add_argument('--gen-model', nargs=1, help='Image generation model (dall-e-3, dall-e-2, or gemini)')
    parser.add_argument('--openai-size', nargs=1, help='Image size for DALL-E models (1024x1024, 1792x1024, or 1024x1792)')
    parser.add_argument('--openai-quality', nargs=1, help='Image quality for DALL-E models (standard or hd)')
    parser.add_argument('--openai-style', nargs=1, help='Image style for DALL-E models (vivid or natural)')
    args = parser.parse_args()

    if args:
        if args.paths is not None and len(args.paths) == 1 and os.path.isdir(args.paths[0]):
            # If a single argument is passed and it's a directory
            directory_path = args.paths[0]
            logger.info("Processing files in directory:")
            process_files(directory_path, args.extensions, F)
        elif args.paths:
            # If multiple file paths are passed
            file_list = args.paths
            logger.info("Processing specific files in list:")
            process_files(file_list, args.extensions, F)

        if args.loglevel:
            loglevel = args.loglevel[0].upper()
            if loglevel == 'DEBUG':
                logger.setLevel(logging.DEBUG)
            elif loglevel == 'INFO':
                logger.setLevel(logging.INFO)
            elif loglevel == 'WARN':
                logger.setLevel(logging.WARN)
            elif loglevel == 'ERROR':
                logger.setLevel(logging.ERROR)
            elif loglevel == 'CRITICAL':
                logger.setLevel(logging.CRITICAL)

    # Precedence order of settings:
    # 1. Environment variables
    # 2. Env file overrides Environment variables
    # 3. Command line arguments override env file and environment variables

    # Load environment variables from env file
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'env')
    if os.path.exists(env_path):
        logger.info(f"Loading environment variables from {env_path}")
        # Read the env file line by line and set environment variables
        with open(env_path, 'r') as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#'):
                    try:
                        key, value = line.split('=', 1)
                        # Remove quotes if present
                        value = value.strip().strip("'\"")
                        os.environ[key.strip()] = value
                        logger.debug(f"Set environment variable: {key}={value}")
                    except ValueError:
                        logger.warning(f"Invalid line in env file: {line}")
    else:
        logger.info(f"No env file found at {env_path}. Using default environment variables.")

    # Log all relevant environment variables
    logger.info("Environment Variables:")
    logger.info(f"  OPENAI_API_KEY: {'Set' if os.environ.get('OPENAI_API_KEY') else 'Not set'}")
    logger.info(f"  GEMINI_API_KEY: {'Set' if os.environ.get('GEMINI_API_KEY') else 'Not set'}")
    logger.info(f"  GEN_MODEL: {os.environ.get('GEN_MODEL', 'Not set (will default to dall-e-3)')}")
    logger.info(f"  OPENAI_MODEL: {os.environ.get('OPENAI_MODEL', 'Not set (using GEN_MODEL instead)')}")
    logger.info(f"  OPENAI_SIZE: {os.environ.get('OPENAI_SIZE', 'Not set (will default to 1024x1024)')}")
    logger.info(f"  OPENAI_QUALITY: {os.environ.get('OPENAI_QUALITY', 'Not set (will default to standard)')}")
    logger.info(f"  OPENAI_STYLE: {os.environ.get('OPENAI_STYLE', 'Not set (will default to vivid)')}")
    logger.info(f"  OPENAI_SYSTEM_PROMPT: {os.environ.get('OPENAI_SYSTEM_PROMPT', 'Not set (will use default)')}")

    # Initialize the gen_config after environment variables are loaded
    logger.info("Initializing image generation configuration...")
    gen_config = initialize_gen_config()
    if gen_config:
        logger.info(f"Image generation model set to: {gen_config.model}")
        logger.info(f"Is Gemini model: {gen_config.is_gemini_model()}")
        logger.info(f"Is OpenAI model: {gen_config.is_openai_model()}")
    else:
        logger.error("Failed to initialize image generation configuration")

    # Args to override settings that were in either the environment or env file 
    if args:
        if args.openai_api_key:
            gen_config.set_api_key(args.openai_api_key[0])
        
        if args.gen_model:
            gen_config.set_model(args.gen_model)
            
        if args.openai_size:
            gen_config.set_size(args.openai_size)
            
        if args.openai_quality:
            gen_config.set_quality(args.openai_quality)
            
        if args.openai_style:
            gen_config.set_style(args.openai_style)

    # Create the server socket with SO_REUSEADDR option
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    
    try:
        server.bind((bind_ip, bind_port))
        server.listen(10)  # max backlog of connections
    except OSError as e:
        logger.error(f"Error binding to {bind_ip}:{bind_port}: {e}")
        logger.error("Port may already be in use. Try killing any existing YAIL processes.")
        sys.exit(1)

    logger.info('='*50)
    logger.info(f'YAIL Server started successfully')
    logger.info(f'Listening on {bind_ip}:{bind_port}')
    
    # Log all available network interfaces to help with debugging
    logger.info('Network information:')
    
    # First try to get the IP using socket connections
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        # Connect to a public DNS server to determine the local IP
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()
        logger.info(f"  Local IP (via socket): {local_ip}")
    except Exception as e:
        logger.warning(f"  Could not determine IP via socket: {e}")
    
    # Try using hostname
    try:
        hostname = socket.gethostname()
        logger.info(f"  Hostname: {hostname}")
        try:
            host_ip = socket.gethostbyname(hostname)
            logger.info(f"  IP Address (via hostname): {host_ip}")
        except Exception as e:
            logger.warning(f"  Could not resolve hostname to IP: {e}")
            logger.info(f"  Using fallback local IP: 127.0.0.1")
    except Exception as e:
        logger.warning(f"  Could not determine hostname: {e}")
        logger.info(f"  Using fallback local IP: 127.0.0.1")
    
    # Try using netifaces if available
    try:
        import netifaces
        logger.info('  Available network interfaces:')
        local_ips = []
        
        for interface in netifaces.interfaces():
            try:
                addresses = netifaces.ifaddresses(interface)
                if netifaces.AF_INET in addresses:
                    for address in addresses[netifaces.AF_INET]:
                        ip = address.get('addr', '')
                        if ip and not ip.startswith('127.'):  # Skip loopback addresses
                            local_ips.append(ip)
                        logger.info(f"    {interface}: {ip or 'unknown'}")
            except Exception as e:
                logger.warning(f"    Error getting info for interface {interface}: {e}")
        
        # Log the best IP to use for connections
        if local_ips:
            logger.info(f"  Recommended IP for connections: {local_ips[0]}")
        else:
            logger.info("  No non-loopback interfaces found, using 127.0.0.1")
    except ImportError:
        logger.warning("  netifaces package not available. Install with: pip install netifaces")
    except Exception as e:
        logger.warning(f"  Error using netifaces: {e}")
    
    logger.info('='*50)

    # Initialize the camera if specified
    if args.camera:
        if not init_camera(args.camera):
            logger.error("Failed to initialize camera. Exiting.")
    else:
        # Try to initialize the default camera
        init_camera()

    while True:
        # Clean up finished threads from the active_threads list
        active_threads[:] = [t for t in active_threads if t.is_alive()]
        
        # Accept new client connections
        client_sock, address = server.accept()
        logger.info(f'Accepted connection from {address[0]}:{address[1]}')
        client_handler = Thread(
            target=handle_client_connection,
            args=(client_sock, len(active_threads) + 1)  # thread_id is 1-based
        )
        client_handler.daemon = True
        client_handler.start()
        active_threads.append(client_handler)

if __name__ == "__main__":
    main()
