#!/bin/bash

# ============================================================
# FTMO Bot Manager
# Usage: ftmo start | ftmo stop | ftmo log | ftmo status
# ============================================================

BOT_SCRIPT="$HOME/Downloads/FTMO_V1.py"
DASH_SCRIPT="$HOME/Downloads/dashboard_api.py"
PYTHON="$HOME/.wine/drive_c/python/python.exe"
WINE="wine"
LOG_FILE="$HOME/Downloads/ftmo_v1.log"
BOT_LOG="$HOME/Downloads/bot_session.log"
DASH_LOG="$HOME/Downloads/dashboard_session.log"

# Colours
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
WHITE='\033[1;37m'
NC='\033[0m' # No colour

banner() {
    echo -e "${CYAN}"
    echo "  ███████╗████████╗███╗   ███╗ ██████╗ "
    echo "  ██╔════╝╚══██╔══╝████╗ ████║██╔═══██╗"
    echo "  █████╗     ██║   ██╔████╔██║██║   ██║"
    echo "  ██╔══╝     ██║   ██║╚██╔╝██║██║   ██║"
    echo "  ██║        ██║   ██║ ╚═╝ ██║╚██████╔╝"
    echo "  ╚═╝        ╚═╝   ╚═╝     ╚═╝ ╚═════╝ "
    echo -e "${WHITE}  FTMO Challenge Bot Manager — V5.4${NC}"
    echo -e "${CYAN}  ════════════════════════════════════${NC}"
    echo ""
}

is_running() {
    pgrep -f "$1" > /dev/null 2>&1
}

get_pid() {
    pgrep -f "$1" 2>/dev/null | head -1
}

start_bot() {
    echo -e "${YELLOW}  ⚡ Starting FTMO Bot...${NC}"
    if is_running "FTMO_V1.py"; then
        echo -e "${YELLOW}  ⚠️  Bot is already running (PID: $(get_pid 'FTMO_V1.py'))${NC}"
    else
        PYTHONIOENCODING=utf-8 PYTHONUTF8=1 $WINE $PYTHON $BOT_SCRIPT \
            >> "$BOT_LOG" 2>&1 &
        sleep 2
        if is_running "FTMO_V1.py"; then
            echo -e "${GREEN}  ✅ Bot started successfully (PID: $(get_pid 'FTMO_V1.py'))${NC}"
        else
            echo -e "${RED}  ❌ Bot failed to start — check $BOT_LOG${NC}"
        fi
    fi
}

start_dashboard() {
    echo -e "${YELLOW}  ⚡ Starting Dashboard...${NC}"
    if is_running "dashboard_api.py"; then
        echo -e "${YELLOW}  ⚠️  Dashboard already running (PID: $(get_pid 'dashboard_api.py'))${NC}"
    else
        PYTHONIOENCODING=utf-8 PYTHONUTF8=1 $WINE $PYTHON $DASH_SCRIPT \
            >> "$DASH_LOG" 2>&1 &
        sleep 3
        if is_running "dashboard_api.py"; then
            echo -e "${GREEN}  ✅ Dashboard started (PID: $(get_pid 'dashboard_api.py'))${NC}"
            echo -e "${CYAN}  🌐 Access at: http://localhost:5001${NC}"
        else
            echo -e "${RED}  ❌ Dashboard failed to start — check $DASH_LOG${NC}"
        fi
    fi
}

stop_bot() {
    echo -e "${YELLOW}  🛑 Shutting down Bot...${NC}"
    if is_running "FTMO_V1.py"; then
        pkill -f "FTMO_V1.py"
        sleep 2
        if is_running "FTMO_V1.py"; then
            pkill -9 -f "FTMO_V1.py"
            echo -e "${RED}  ⚠️  Bot force-killed${NC}"
        else
            echo -e "${GREEN}  ✅ Bot shut down cleanly${NC}"
        fi
    else
        echo -e "${WHITE}  ℹ️  Bot is not running${NC}"
    fi
}

stop_dashboard() {
    echo -e "${YELLOW}  🛑 Shutting down Dashboard...${NC}"
    if is_running "dashboard_api.py"; then
        pkill -f "dashboard_api.py"
        sleep 2
        if is_running "dashboard_api.py"; then
            pkill -9 -f "dashboard_api.py"
            echo -e "${RED}  ⚠️  Dashboard force-killed${NC}"
        else
            echo -e "${GREEN}  ✅ Dashboard shut down cleanly${NC}"
        fi
    else
        echo -e "${WHITE}  ℹ️  Dashboard is not running${NC}"
    fi
}

show_status() {
    echo -e "${WHITE}  📊 System Status${NC}"
    echo -e "${CYAN}  ─────────────────────────────────────${NC}"

    if is_running "FTMO_V1.py"; then
        PID=$(get_pid "FTMO_V1.py")
        UPTIME=$(ps -p $PID -o etime= 2>/dev/null | tr -d ' ')
        echo -e "${GREEN}  ● Bot        RUNNING${NC} | PID: $PID | Uptime: $UPTIME"
    else
        echo -e "${RED}  ○ Bot        STOPPED${NC}"
    fi

    if is_running "dashboard_api.py"; then
        PID=$(get_pid "dashboard_api.py")
        UPTIME=$(ps -p $PID -o etime= 2>/dev/null | tr -d ' ')
        echo -e "${GREEN}  ● Dashboard  RUNNING${NC} | PID: $PID | Uptime: $UPTIME"
        echo -e "${CYAN}  🌐 http://localhost:5001${NC}"
    else
        echo -e "${RED}  ○ Dashboard  STOPPED${NC}"
    fi

    echo -e "${CYAN}  ─────────────────────────────────────${NC}"

    # Last heartbeat
    if [ -f "$HOME/Downloads/bot_heartbeat.log" ]; then
        LAST_HB=$(tail -1 "$HOME/Downloads/bot_heartbeat.log" 2>/dev/null)
        if [ -n "$LAST_HB" ]; then
            echo -e "${WHITE}  💓 Last heartbeat: ${NC}$(echo $LAST_HB | cut -c1-60)"
        fi
    fi

    # Today's equity from log
    TODAY_EQUITY=$(grep "\[HEARTBEAT\]" "$LOG_FILE" 2>/dev/null | tail -1 | grep -o "Equity:[^|]*" | head -1)
    if [ -n "$TODAY_EQUITY" ]; then
        echo -e "${WHITE}  💷 $TODAY_EQUITY${NC}"
    fi
}

show_log() {
    echo -e "${WHITE}  📋 Notable Events (last 50)${NC}"
    echo -e "${CYAN}  ─────────────────────────────────────${NC}"
    echo ""

    # Filter for notable events only
    cat ~/Downloads/ftmo_v1.log.* ~/Downloads/ftmo_v1.log 2>/dev/null | \
    grep -E "\[OPEN\b|\[CLOSE\]|\[BE_LOCK\]|\[TRAIL\]|\[ZONE_INV\]|\[TIME_STOP\]|\[LOCK_IN\]|\[BASKET\]|\[DAILY\]|\[HALT\]|\[GOLD_REGIME\]|\[CLAUDE_HARD_GATE\]|\[OPPORTUNITY_SCAN\]|\[SESSION_BRIEF\]|\[TRADE_RESULT\]|\[REJECTED_SIGNAL\]|\[GBPJPY_SHADOW\]|\[HEARTBEAT\]" | \
    grep -v "DEBUG" | \
    tail -50 | \
        while IFS= read -r line; do
            if echo "$line" | grep -q "OPEN|"; then
                echo -e "${GREEN}  $line${NC}"
            elif echo "$line" | grep -q "\[CLOSE\]"; then
                echo -e "${CYAN}  $line${NC}"
            elif echo "$line" | grep -q "HALT\|ERROR\|DAILY_DD"; then
                echo -e "${RED}  $line${NC}"
            elif echo "$line" | grep -q "CLAUDE_HARD_GATE\|OPPORTUNITY_SCAN\|SESSION_BRIEF"; then
                echo -e "${YELLOW}  $line${NC}"
            elif echo "$line" | grep -q "TRADE_RESULT\|GOLD_REGIME"; then
                echo -e "${WHITE}  $line${NC}"
            else
                echo "  $line"
            fi
        done
    echo ""
}

restart_all() {
    echo -e "${YELLOW}  🔄 Restarting all services...${NC}"
    echo ""
    stop_bot
    stop_dashboard
    echo ""
    sleep 2
    start_bot
    start_dashboard
    echo ""
    show_status
}

# ============================================================
# MAIN
# ============================================================

banner

case "$1" in
    start)
        echo -e "${WHITE}  Action: START${NC}"
        echo ""
        start_bot
        start_dashboard
        echo ""
        show_status
        echo ""
        echo -e "${YELLOW}  🌐 Resetting browser to dashboard only...${NC}"
        pkill -9 brave 2>/dev/null
        sleep 2
        brave-browser --new-window http://localhost:5001 \
            --no-first-run \
            --disable-background-networking \
            --disable-background-timer-throttling \
            --disable-extensions \
            --disable-background-mode \
            2>/dev/null &
        echo -e "${GREEN}  ✅ Browser opened — dashboard only${NC}"
        ;;
    stop)
        echo -e "${WHITE}  Action: STOP${NC}"
        echo ""
        stop_bot
        stop_dashboard
        echo ""
        echo -e "${GREEN}  ✅ All services stopped cleanly${NC}"
        ;;
    restart)
        echo -e "${WHITE}  Action: RESTART${NC}"
        echo ""
        restart_all
        ;;
    status)
        show_status
        ;;
    log)
        show_log
        ;;
    bot)
        echo -e "${WHITE}  Action: BOT ONLY${NC}"
        echo ""
        start_bot
        ;;
    dashboard)
        echo -e "${WHITE}  Action: DASHBOARD ONLY${NC}"
        echo ""
        start_dashboard
        ;;
    browser)
        echo -e "${WHITE}  Action: BROWSER RESET${NC}"
        echo ""
        echo -e "${YELLOW}  🛑 Closing all Brave processes...${NC}"
        pkill -9 brave 2>/dev/null
        sleep 3
        COUNT=$(pgrep brave | wc -l)
        if [ "$COUNT" -gt "0" ]; then
            echo -e "${RED}  ⚠️  $COUNT Brave processes still running — force killing...${NC}"
            pkill -9 -f brave 2>/dev/null
            sleep 2
        fi
        echo -e "${GREEN}  ✅ All Brave processes stopped${NC}"
        echo ""
        echo -e "${YELLOW}  ⚡ Opening dashboard in clean Brave window...${NC}"
        brave-browser --new-window http://localhost:5001 \
            --no-first-run \
            --disable-background-networking \
            --disable-background-timer-throttling \
            --disable-extensions \
            --disable-background-mode \
            --disable-sync \
            2>/dev/null &
        sleep 3
        COUNT=$(pgrep brave | wc -l)
        echo -e "${GREEN}  ✅ Browser reset complete | $COUNT processes (normal)${NC}"
        echo -e "${CYAN}  🌐 Dashboard: http://localhost:5001${NC}"
        ;;
    *)
        echo -e "${WHITE}  Usage:${NC}"
        echo ""
        echo -e "${CYAN}  ftmo start${NC}      — Start bot + dashboard"
        echo -e "${CYAN}  ftmo stop${NC}       — Stop bot + dashboard cleanly"
        echo -e "${CYAN}  ftmo restart${NC}    — Restart both services"
        echo -e "${CYAN}  ftmo status${NC}     — Show running status + last heartbeat"
        echo -e "${CYAN}  ftmo log${NC}        — Show last 50 notable events"
        echo -e "${CYAN}  ftmo bot${NC}        — Start bot only"
        echo -e "${CYAN}  ftmo dashboard${NC}  — Start dashboard only"
        echo -e "${CYAN}  ftmo browser${NC}   — Reset browser to dashboard only"
        echo ""
        ;;
esac

echo ""
