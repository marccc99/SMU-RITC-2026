# ==========================================
# MARC'S Strat 7 - Skew Trading with Optimized Sizing and Extra-Agressive Risk Management
# ==========================================

import requests
import time
import sys
import threading
import queue
import tkinter as tk
from tkinter import scrolledtext, ttk

# ==========================================
# CONFIGURATION (TRIPLE-TAP OPTIMAL)
# ==========================================
API_KEY = 'JJXREJ93'
BASE_URL = 'http://localhost:9999/v1'
HEADERS = {'X-API-Key': API_KEY}

TICKERS = ['WNTR', 'SMMR']

# --- THE GOLDEN RATIO ---
# Size 7,400 allows us to hold 3 layers (22,200) within the 22,600 cap.
# And 22,600 + 7,400 (Ghost Fill) = 30,000 (Exact Limit).
MAX_ORDER_SIZE = 7200        
SAFETY_NET_CAP = 22600       
SAFETY_GROSS_CAP = 41800     
MAX_SINGLE_ORDER = 10000     

# --- 30% TARGETS ---
TARGET_GROSS = SAFETY_GROSS_CAP * 0.30  
TARGET_NET = SAFETY_NET_CAP * 0.30      

# STRAT PARAMETERS
BASE_HALF_SPREAD = 0.01    
PUSH_COEFF = 0.04          
PULL_COEFF = 0.02          
DEFENSIVE_PULL = 0.5       
MIN_MKT_SPREAD = 0.02      

# HIGH SPEED
LOOP_SPEED = 0.13            
TICKER_DELAY = 0.10          

# AUTO-CRUSH
DANGER_THRESHOLD = 20000     # Lowered slightly for the new sizing
CRUSH_SKEW = 0.05            

# Global State
shadow_pos = {t: 0 for t in TICKERS}
last_quotes = {t: {'buy': 0, 'sell': 0} for t in TICKERS}
price_hist = {t: [] for t in TICKERS}

# GUI Shared Data
gui_data = {
    'WNTR_POS': 0, 'WNTR_ORD': 0,
    'SMMR_POS': 0, 'SMMR_ORD': 0,
    'NET': 0, 'GROSS': 0,
    'PNL': 0.0,
    'STATUS': 'IDLE'
}

stop_event = threading.Event()
trim_event = threading.Event()
log_queue = queue.Queue()

# ==========================================
# HELPER FUNCTIONS
# ==========================================
def log(msg):
    log_queue.put(msg)

def get_book(ticker):
    try: return requests.get(f"{BASE_URL}/securities/book", headers=HEADERS, params={'ticker': ticker}, timeout=0.1).json()
    except: return None

def post_order_optimistic(ticker, action, quantity, type, price=None, tag="OPEN"):
    global shadow_pos
    if quantity < 1: return
    rem = int(quantity)
    
    while rem > 0:
        chunk = min(rem, MAX_SINGLE_ORDER)
        params = {'ticker': ticker, 'action': action, 'quantity': chunk, 'type': type}
        if price: params['price'] = price
        try:
            resp = requests.post(f"{BASE_URL}/orders", headers=HEADERS, params=params)
            if resp.status_code == 200:
                shadow_pos[ticker] += chunk if action == 'BUY' else -chunk
                log(f"  [{tag}] {action} {chunk} {ticker} @ {price}")
                gui_data[f'{ticker}_ORD'] = gui_data.get(f'{ticker}_ORD', 0) + chunk
            else:
                log(f"  [REJ] {resp.status_code} {action} {chunk}")
        except Exception as e:
            log(f"  [ERR] Network: {str(e)}")
        rem -= chunk

# ==========================================
# CORE TRADING LOGIC
# ==========================================
def market_make_ticker(ticker_info, sec):
    global shadow_pos, last_quotes, price_hist
    ticker = ticker_info['ticker']
    server_pos = int(ticker_info['position'])
    
    # 1. SYNC & GUI
    try:
        open_orders = requests.get(f"{BASE_URL}/orders", headers=HEADERS, params={'status': 'OPEN'}).json()
        raw_open_vol = sum((o['quantity'] - o['filled']) for o in open_orders if o['ticker'] == ticker)
        gui_data[f'{ticker}_ORD'] = raw_open_vol 
        
        net_open_shadow = sum((o['quantity'] - o['filled']) if o['action'] == 'BUY' else -(o['quantity'] - o['filled']) 
                       for o in open_orders if o['ticker'] == ticker)
        shadow_pos[ticker] = server_pos + net_open_shadow
    except: 
        shadow_pos[ticker] = server_pos

    # 2. URGENCY
    is_urgent_dump = (abs(server_pos) > DANGER_THRESHOLD) or (sec >= 58) or (sec < 2)
    is_risk_reduction = (sec >= 52 or sec < 3)

    book = get_book(ticker)
    if not book or not book.get('bids'): return
    
    bid_px = book['bids'][0]['price']
    ask_px = book['asks'][0]['price']
    mid_price = (bid_px + ask_px) / 2.0
    
    # --- AUTO-CRUSH ---
    if is_urgent_dump and server_pos != 0:
        action = 'SELL' if server_pos > 0 else 'BUY'
        crush_price = round(bid_px - CRUSH_SKEW, 2) if action == 'SELL' else round(ask_px + CRUSH_SKEW, 2)
        
        requests.post(f"{BASE_URL}/commands/cancel", headers=HEADERS, params={'ticker': ticker})
        gui_data[f'{ticker}_ORD'] = 0
        
        dump_qty = abs(server_pos)
        post_order_optimistic(ticker, action, dump_qty, 'LIMIT', crush_price, tag="DUMP")
        return 
    # ------------------

    # Standard Market Making
    price_hist[ticker].append(mid_price)
    if len(price_hist[ticker]) > 10: price_hist[ticker].pop(0)
    vol_adj = min(0.06, (max(price_hist[ticker]) - min(price_hist[ticker])) * 0.5) if len(price_hist[ticker]) > 1 else 0

    current_pull = DEFENSIVE_PULL if is_risk_reduction else PULL_COEFF
    inv_ratio = shadow_pos[ticker] / (SAFETY_NET_CAP / 2)
    
    if inv_ratio > 0:
        bid_h = BASE_HALF_SPREAD + vol_adj + (PUSH_COEFF * inv_ratio)
        ask_h = max(0.01, BASE_HALF_SPREAD + vol_adj - (current_pull * inv_ratio))
    else:
        ask_h = BASE_HALF_SPREAD + vol_adj + (PUSH_COEFF * abs(inv_ratio))
        bid_h = max(0.01, BASE_HALF_SPREAD + vol_adj - (current_pull * abs(inv_ratio)))

    t_bid, t_ask = round(mid_price - bid_h, 2), round(mid_price + ask_h, 2)
    if (t_ask - t_bid) < MIN_MKT_SPREAD: t_ask = t_bid + MIN_MKT_SPREAD

    if abs(t_bid - last_quotes[ticker]['buy']) < 0.01 and abs(t_ask - last_quotes[ticker]['sell']) < 0.01:
        return

    requests.post(f"{BASE_URL}/commands/cancel", headers=HEADERS, params={'ticker': ticker})
    gui_data[f'{ticker}_ORD'] = 0
    shadow_pos[ticker] = server_pos 
    
    last_quotes[ticker]['buy'], last_quotes[ticker]['sell'] = t_bid, t_ask
    
    if is_risk_reduction:
        unwind_qty = abs(server_pos)
        if server_pos < 0: post_order_optimistic(ticker, 'BUY', unwind_qty, 'LIMIT', t_bid)
        if server_pos > 0: post_order_optimistic(ticker, 'SELL', unwind_qty, 'LIMIT', t_ask)
    else:
        total_gross = sum(abs(v) for v in shadow_pos.values())
        remaining_gross = SAFETY_GROSS_CAP - total_gross
        total_portfolio_net = sum(shadow_pos.values())
        
        # DYNAMIC SCALING
        current_size = MAX_ORDER_SIZE
        # If we are holding 2 layers (14.8k), scale down to 1/3 size for the last layer
        if abs(shadow_pos[ticker]) >= (SAFETY_NET_CAP * 0.65):
            current_size = MAX_ORDER_SIZE // 3

        if remaining_gross > 0:
            qty = min(current_size, remaining_gross // 2)
            f_buy = int(max(0, min(qty, SAFETY_NET_CAP - total_portfolio_net)))
            f_sell = int(max(0, min(qty, SAFETY_NET_CAP + total_portfolio_net)))
            
            if f_buy > 0: post_order_optimistic(ticker, 'BUY', f_buy, 'LIMIT', t_bid)
            if f_sell > 0: post_order_optimistic(ticker, 'SELL', f_sell, 'LIMIT', t_ask)
        else:
            # OVERFLOW UNWIND
            unwind_qty = min(MAX_ORDER_SIZE, abs(server_pos))
            if server_pos > 0: post_order_optimistic(ticker, 'SELL', unwind_qty, 'LIMIT', t_ask, tag="UNWIND")
            elif server_pos < 0: post_order_optimistic(ticker, 'BUY', unwind_qty, 'LIMIT', t_bid, tag="UNWIND")

# ==========================================
# TRIM ROUTINE
# ==========================================
def run_trim_routine():
    log("\n>>> ACTIVATING 30% TRIM <<<")
    gui_data['STATUS'] = 'TRIMMING'
    
    while not stop_event.is_set():
        try:
            securities = requests.get(f"{BASE_URL}/securities", headers=HEADERS, timeout=0.5).json()
            current_gross = sum(abs(s['position']) for s in securities if s['ticker'] in TICKERS)
            current_net = sum(s['position'] for s in securities if s['ticker'] in TICKERS)

            # Update GUI
            pnl = sum((s['realized'] + s['unrealized']) for s in securities if s['ticker'] in TICKERS)
            gui_data['PNL'] = pnl
            gui_data['GROSS'] = current_gross
            gui_data['NET'] = current_net
            for s in securities:
                if s['ticker'] in TICKERS: gui_data[f"{s['ticker']}_POS"] = s['position']

            log(f"[TRIM] Gross: {current_gross} | Net: {current_net}")
            
            if current_gross <= TARGET_GROSS and abs(current_net) <= TARGET_NET:
                log(">>> 30% SAFE ZONE REACHED. RESUMING. <<<")
                return 

            for s_info in securities:
                ticker = s_info['ticker']
                if ticker not in TICKERS: continue
                pos = int(s_info['position'])
                if pos == 0: continue
                
                try:
                    book = requests.get(f"{BASE_URL}/securities/book", headers=HEADERS, params={'ticker': ticker}, timeout=0.2).json()
                except: continue

                bid_px = book['bids'][0]['price']
                ask_px = book['asks'][0]['price']
                
                action = 'SELL' if pos > 0 else 'BUY'
                price = round(bid_px - 0.05, 2) if action == 'SELL' else round(ask_px + 0.05, 2)
                
                requests.post(f"{BASE_URL}/commands/cancel", headers=HEADERS, params={'ticker': ticker})
                
                qty_left = abs(pos)
                while qty_left > 0:
                    chunk = min(qty_left, MAX_SINGLE_ORDER)
                    log(f" -> TRIM {ticker}: {action} {chunk} @ {price}")
                    try:
                        requests.post(f"{BASE_URL}/orders", headers=HEADERS, params={
                            'ticker': ticker, 'action': action, 'quantity': chunk, 'type': 'LIMIT', 'price': price
                        })
                    except: pass
                    
                    qty_left -= chunk
                    time.sleep(0.05) 
                
            time.sleep(0.5)
        except Exception as e:
            log(f"Trim Err: {e}")
            time.sleep(1)

def trading_loop():
    log("=== SYSTEM STARTED (TRIPLE-TAP MODE) ===")
    gui_data['STATUS'] = 'RUNNING'
    while not stop_event.is_set():
        if trim_event.is_set():
            run_trim_routine() 
            trim_event.clear() 
            gui_data['STATUS'] = 'RUNNING' 
            log("=== RESUMED ===")
            time.sleep(0.5)
            continue 

        try:
            case = requests.get(f"{BASE_URL}/case", headers=HEADERS).json()
            if case['status'] != 'ACTIVE': 
                time.sleep(1); continue
            
            securities = requests.get(f"{BASE_URL}/securities", headers=HEADERS).json()
            
            agg_net = 0
            agg_gross = 0
            pnl_total = 0.0
            
            for s in securities:
                if s['ticker'] in TICKERS:
                    gui_data[f"{s['ticker']}_POS"] = int(s['position'])
                    agg_net += int(s['position'])
                    agg_gross += abs(int(s['position']))
                    pnl_total += (s['realized'] + s['unrealized'])
            
            gui_data['NET'] = agg_net
            gui_data['GROSS'] = agg_gross
            gui_data['PNL'] = pnl_total

            for t_name in TICKERS:
                s_info = next((s for s in securities if s['ticker'] == t_name), None)
                if s_info: 
                    market_make_ticker(s_info, case['tick'] % 60)
                    time.sleep(TICKER_DELAY) 

            time.sleep(LOOP_SPEED)
        except Exception as e:
            time.sleep(1)
    
    gui_data['STATUS'] = 'STOPPED'
    log("=== SYSTEM STOPPED ===")

# ==========================================
# GUI IMPLEMENTATION
# ==========================================
class TradingApp:
    def __init__(self, root):
        self.root = root
        self.root.title("RIT COMMANDER v1021 (TRIPLE-TAP)")
        self.root.geometry("600x700")
        
        style = ttk.Style()
        style.configure("Bold.TButton", font=('Helvetica', 10, 'bold'))

        self.header = tk.Frame(root, pady=10)
        self.header.pack()
        self.lbl_status = tk.Label(self.header, text="STATUS: IDLE", fg="gray", font=("Consolas", 16, "bold"))
        self.lbl_status.pack()

        # P&L Frame
        self.pnl_frame = tk.Frame(root, pady=5)
        self.pnl_frame.pack()
        self.lbl_pnl = tk.Label(self.pnl_frame, text="P&L: $0.00", font=("Consolas", 20, "bold"), fg="black")
        self.lbl_pnl.pack()

        # Monitor Frame
        self.monitor_frame = tk.LabelFrame(root, text=" Live Portfolio ", font=("Helvetica", 10, "bold"))
        self.monitor_frame.pack(padx=10, pady=5, fill="x")
        
        tk.Label(self.monitor_frame, text="Ticker", font=("Consolas", 10, "bold")).grid(row=0, column=0)
        tk.Label(self.monitor_frame, text="Position", font=("Consolas", 10, "bold")).grid(row=0, column=1)
        tk.Label(self.monitor_frame, text="Open Orders", font=("Consolas", 10, "bold")).grid(row=0, column=2)

        tk.Label(self.monitor_frame, text="WNTR", font=("Consolas", 12)).grid(row=1, column=0, padx=10)
        self.lbl_wntr_pos = tk.Label(self.monitor_frame, text="0", font=("Consolas", 12))
        self.lbl_wntr_pos.grid(row=1, column=1, padx=10)
        self.lbl_wntr_ord = tk.Label(self.monitor_frame, text="0", font=("Consolas", 12), fg="orange")
        self.lbl_wntr_ord.grid(row=1, column=2, padx=10)

        tk.Label(self.monitor_frame, text="SMMR", font=("Consolas", 12)).grid(row=2, column=0, padx=10)
        self.lbl_smmr_pos = tk.Label(self.monitor_frame, text="0", font=("Consolas", 12))
        self.lbl_smmr_pos.grid(row=2, column=1, padx=10)
        self.lbl_smmr_ord = tk.Label(self.monitor_frame, text="0", font=("Consolas", 12), fg="orange")
        self.lbl_smmr_ord.grid(row=2, column=2, padx=10)

        self.stats_frame = tk.Frame(root)
        self.stats_frame.pack(pady=5)
        self.lbl_net = tk.Label(self.stats_frame, text="NET: 0", font=("Consolas", 14, "bold"), fg="blue", width=15)
        self.lbl_net.pack(side=tk.LEFT, padx=10)
        self.lbl_gross = tk.Label(self.stats_frame, text="GROSS: 0", font=("Consolas", 14, "bold"), fg="purple", width=15)
        self.lbl_gross.pack(side=tk.LEFT, padx=10)

        self.controls = tk.Frame(root, pady=15)
        self.controls.pack()
        self.btn_start = ttk.Button(self.controls, text="START TRADING", style="Bold.TButton", command=self.start_trading)
        self.btn_start.grid(row=0, column=0, padx=10)
        self.btn_stop = ttk.Button(self.controls, text="STOP", command=self.stop_trading)
        self.btn_stop.grid(row=0, column=1, padx=10)
        self.btn_trim = tk.Button(self.controls, text="TRIM TO 30%", bg="#ffcccc", fg="red", font=("Helvetica", 10, "bold"), height=2, command=self.trigger_trim)
        self.btn_trim.grid(row=0, column=2, padx=20)

        self.log_area = scrolledtext.ScrolledText(root, width=70, height=18, font=("Consolas", 9))
        self.log_area.pack(padx=10, pady=10)
        self.log_area.tag_config('error', foreground='red')
        self.log_area.tag_config('ok', foreground='green')
        self.log_area.tag_config('open', foreground='blue')
        self.log_area.tag_config('dump', foreground='magenta')

        self.worker_thread = None
        self.check_queue()
        self.update_monitor()

    def update_monitor(self):
        self.lbl_wntr_pos.config(text=f"{gui_data.get('WNTR_POS', 0)}")
        self.lbl_wntr_ord.config(text=f"{gui_data.get('WNTR_ORD', 0)}")
        self.lbl_smmr_pos.config(text=f"{gui_data.get('SMMR_POS', 0)}")
        self.lbl_smmr_ord.config(text=f"{gui_data.get('SMMR_ORD', 0)}")
        
        net = gui_data.get('NET', 0)
        gross = gui_data.get('GROSS', 0)
        pnl = gui_data.get('PNL', 0.0)
        
        self.lbl_net.config(text=f"NET: {net}")
        self.lbl_gross.config(text=f"GROSS: {gross}")
        self.lbl_pnl.config(text=f"P&L: ${pnl:,.2f}")
        
        if pnl >= 0: self.lbl_pnl.config(fg="green")
        else: self.lbl_pnl.config(fg="red")

        if abs(net) > 25000: self.lbl_net.config(fg="red")
        else: self.lbl_net.config(fg="blue")
        if gross > 40000: self.lbl_gross.config(fg="red")
        else: self.lbl_gross.config(fg="purple")

        status = gui_data.get('STATUS', 'IDLE')
        self.lbl_status.config(text=f"STATUS: {status}")
        if status == 'RUNNING': self.lbl_status.config(fg="green")
        elif status == 'TRIMMING': self.lbl_status.config(fg="red")
        else: self.lbl_status.config(fg="gray")

        self.root.after(200, self.update_monitor)

    def log_gui(self, msg):
        self.log_area.insert(tk.END, msg + "\n")
        if "[ERR]" in msg or "[REJ]" in msg: self.log_area.tag_add('error', "end-2l", "end-1l")
        if "[OK]" in msg: self.log_area.tag_add('ok', "end-2l", "end-1l")
        if "[OPEN]" in msg: self.log_area.tag_add('open', "end-2l", "end-1l")
        if "[DUMP]" in msg: self.log_area.tag_add('dump', "end-2l", "end-1l")
        self.log_area.see(tk.END)

    def check_queue(self):
        while not log_queue.empty():
            msg = log_queue.get()
            self.log_gui(msg)
        self.root.after(100, self.check_queue)

    def start_trading(self):
        if self.worker_thread and self.worker_thread.is_alive(): return
        stop_event.clear()
        trim_event.clear()
        self.worker_thread = threading.Thread(target=trading_loop, daemon=True)
        self.worker_thread.start()

    def stop_trading(self):
        stop_event.set()

    def trigger_trim(self):
        trim_event.set()

if __name__ == "__main__":
    root = tk.Tk()
    app = TradingApp(root)
    root.mainloop()