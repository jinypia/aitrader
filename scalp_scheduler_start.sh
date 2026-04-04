#!/bin/bash
# scalp_scheduler_start.sh - Start scalping scheduler with optional test mode

set -e

AITRADER_DIR="/Users/superarchi/aitrader"
cd "$AITRADER_DIR"

# Colors for output
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
RED='\033[0;31m'
NC='\033[0m' # No Color

echo -e "${BLUE}═══════════════════════════════════════════════════${NC}"
echo -e "${BLUE}  PROFESSIONAL SCALPING SCHEDULER${NC}"
echo -e "${BLUE}═══════════════════════════════════════════════════${NC}\n"

# Check Slack configuration
if [ -z "$SLACK_WEBHOOK_URL" ]; then
    echo -e "${RED}⚠️  Slack not configured!${NC}"
    echo -e "${YELLOW}Run: ./setup_slack.sh${NC}"
    echo -e "${YELLOW}Or manually set SLACK_WEBHOOK_URL environment variable${NC}\n"
    echo -e "${YELLOW}Continuing without Slack notifications...${NC}\n"
else
    echo -e "${GREEN}✅ Slack integration enabled${NC}"
    echo -e "${BLUE}📱 Channel: ${SLACK_CHANNEL:-#trading-reports}${NC}\n"
fi

# Parse arguments
if [ "$1" = "test" ] || [ "$1" = "--test" ]; then
    echo -e "${YELLOW}Running in TEST mode (full daily cycle simulation)${NC}\n"
    python scalp_scheduler.py --full
    
elif [ "$1" = "validate" ] || [ "$1" = "--validate" ]; then
    echo -e "${YELLOW}Running PRE-MARKET VALIDATION${NC}\n"
    python scalp_scheduler.py --validate
    
elif [ "$1" = "select" ] || [ "$1" = "--select" ]; then
    echo -e "${YELLOW}Selecting best stocks now${NC}\n"
    python scalp_scheduler.py --select
    
elif [ "$1" = "session" ] || [ "$1" = "--session" ]; then
    echo -e "${YELLOW}Running one scalping session${NC}\n"
    python scalp_scheduler.py --session
    
elif [ "$1" = "check" ] || [ "$1" = "--check" ]; then
    echo -e "${YELLOW}Checking scheduler status${NC}\n"
    python scalp_scheduler.py --check
    
elif [ "$1" = "start" ] || [ "$1" = "--start" ] || [ -z "$1" ]; then
    echo -e "${GREEN}Starting 24/7 automation scheduler...${NC}"
    echo -e "${BLUE}Schedule:${NC}"
    echo -e "  • 08:30 AM (KST) - Pre-market validation"
    echo -e "  • 09:00 AM (KST) - Market open + stock selection"
    echo -e "  • 11:00 AM (KST) - Refresh stocks + session"
    echo -e "  • 13:00 PM (KST) - Refresh stocks + session"
    echo -e "  • 15:00 PM (KST) - Final refresh + session"
    echo -e "  • 15:30 PM (KST) - Market close summary"
    echo -e "\n${YELLOW}Running on weekdays only...${NC}"
    echo -e "${BLUE}Press Ctrl+C to stop${NC}\n"
    
    python scalp_scheduler.py --start
    
else
    echo -e "${YELLOW}Usage:${NC}"
    echo -e "  ${GREEN}./scalp_scheduler_start.sh${NC} [command]"
    echo -e "\n${BLUE}Commands:${NC}"
    echo -e "  ${GREEN}start${NC}      - Start 24/7 scheduler (default)"
    echo -e "  ${GREEN}test${NC}       - Test full daily cycle"
    echo -e "  ${GREEN}validate${NC}   - Run pre-market validation"
    echo -e "  ${GREEN}select${NC}     - Display best stocks"
    echo -e "  ${GREEN}session${NC}    - Run one trading session"
    echo -e "  ${GREEN}check${NC}      - Check scheduler status"
    echo -e "\n${BLUE}Examples:${NC}"
    echo -e "  ${GREEN}./scalp_scheduler_start.sh${NC}           # Start scheduler"
    echo -e "  ${GREEN}./scalp_scheduler_start.sh test${NC}      # Test mode"
    echo -e "  ${GREEN}./scalp_scheduler_start.sh validate${NC}  # Pre-market validation"
    exit 0
fi
