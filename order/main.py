import json
import time
import uuid
from pathlib import Path
from fastapi import FastAPI, HTTPException
from datetime import datetime, timezone
from enum import Enum

app = FastAPI(title = "TicketMEI - Order")

Max-Tickets = 4 # limites de tickets por pedido



