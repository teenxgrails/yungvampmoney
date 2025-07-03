import datetime
import logging
import sqlite3
import pytz
import subprocess
import matplotlib.pyplot as plt
import calendar
import json
import requests
import os
from matplotlib.ticker import FuncFormatter
from io import BytesIO
from telegram import (
    Update,
    ReplyKeyboardMarkup,
    InlineKeyboardMarkup,
    InlineKeyboardButton
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    ConversationHandler,
    CallbackQueryHandler,
    filters,
    JobQueue
)

# --- Database Setup --- #
def detect_category(description):
    """Auto-detect category based on keywords in description"""
    if not description:
        return 'Other'
    
    description_lower = description.lower()
    for category, keywords in CATEGORIES.items():
        if any(keyword in description_lower for keyword in keywords):
            return category
    return 'Other'

def get_db_connection():
    conn = sqlite3.connect('budget.db', check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn

def init_db():
    with get_db_connection() as conn:
        cursor = conn.cursor()
        
        # Create categories table
        cursor.execute('''CREATE TABLE IF NOT EXISTS categories
                       (id INTEGER PRIMARY KEY, name TEXT UNIQUE)''')
        
        # Insert default categories
        for category in CATEGORIES:
            cursor.execute("INSERT OR IGNORE INTO categories (name) VALUES (?)", (category,))
        
        # Create other tables
        cursor.execute('''CREATE TABLE IF NOT EXISTS transactions
                       (id INTEGER PRIMARY KEY, 
                        user_id INTEGER, 
                        type TEXT, 
                        amount REAL,
                        description TEXT, 
                        date TEXT, 
                        currency TEXT, 
                        category_id INTEGER,
                        FOREIGN KEY(category_id) REFERENCES categories(id))''')
                        
        cursor.execute('''CREATE TABLE IF NOT EXISTS wallets (
                       id INTEGER PRIMARY KEY,
                       user_id INTEGER,
                       name TEXT,
                       currency TEXT,
                       balance REAL DEFAULT 0,
                       is_default INTEGER DEFAULT 0)''')
        
        # Check if wallet_id column exists in transactions
        cursor.execute("PRAGMA table_info(transactions)")
        columns = [row[1] for row in cursor.fetchall()]
        if 'wallet_id' not in columns:
            cursor.execute("ALTER TABLE transactions ADD COLUMN wallet_id INTEGER")
            
        cursor.execute('''CREATE TABLE IF NOT EXISTS holds
                     (id INTEGER PRIMARY KEY, user_id INTEGER, amount REAL, description TEXT, currency TEXT)''')
        cursor.execute('''CREATE TABLE IF NOT EXISTS recurring
                     (id INTEGER PRIMARY KEY, user_id INTEGER, type TEXT, amount REAL,
                     description TEXT, currency TEXT, day_of_month INTEGER,
                     is_active INTEGER DEFAULT 1)''')
        cursor.execute('''CREATE TABLE IF NOT EXISTS user_settings
                     (user_id INTEGER PRIMARY KEY, default_currency TEXT)''')
        cursor.execute('''CREATE TABLE IF NOT EXISTS budgets
                     (id INTEGER PRIMARY KEY, user_id INTEGER, category_id INTEGER, 
                     amount REAL, currency TEXT, month INTEGER, year INTEGER)''')
        
        conn.commit()

# --- Constants --- #
MAIN_MENU, ADD_INCOME, ADD_OUTCOME, ADD_HOLD, MANAGE_HOLDS, \
RECURRING_MENU, ADD_RECURRING, CURRENCY_MENU, MANAGE_TRANSACTIONS, \
SET_BUDGET, REPORT_MENU, BACKUP, WALLET_MENU, ADD_WALLET, SETTINGS_MENU = range(15)

OXR_API_KEY = '390ab2a864c98873f38df1de'
CURRENCY_API = f"https://open.er-api.com/v6/latest/USD?apikey={OXR_API_KEY}"
TIMEZONE = pytz.timezone('Europe/Moscow')
REPORT_TYPES = ['Monthly Summary', 'Category Breakdown', 'Income vs Expenses']

CURRENCIES = {
    'USD': '$',
    'EUR': '€',
    'CHF': 'Fr',
    'GBP': '£',
    'JPY': '¥',
    'RUB': '₽'
}

CATEGORIES = {
    'Food': ['mcdonalds', 'burger', 'restaurant', 'cafe', 'groceries', 'food', 'eat'],
    'Transport': ['taxi', 'uber', 'metro', 'transport', 'gas', 'fuel', 'parking'],
    'Housing': ['rent', 'mortgage', 'utilities', 'electricity', 'water', 'internet'],
    'Entertainment': ['movie', 'netflix', 'concert', 'game', 'hobby'],
    'Healthcare': ['pharmacy', 'doctor', 'hospital', 'medicine', 'insurance'],
    'Income': ['salary', 'bonus', 'freelance', 'payment', 'invoice'],
    'Other': []
}

# Keyboard layouts
main_keyboard = [
    ['💰 Balance'],
    ['📥 Income', '📤 Outcome'],
    ['⏳ Hold'],
    ['⚙️ Settings']
]

settings_keyboard = [
    ['💱 Currency', '👛 Wallets'],
    ['🔄 Recurring', '📊 Budget'],
    ['📈 Report', '📦 Backup'],
    ['🗑 Transactions'],
    ['🔙 Back']
]
cancel_keyboard = [['❌ Cancel']]

# --- Logging Setup --- #
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- Helper Functions --- #
def format_transaction_date_long(db_date):
    if not db_date:
        return "no date"
    try:
        dt = datetime.datetime.strptime(db_date, "%Y-%m-%d %H:%M:%S")
        return dt.strftime("%Y-%m-%d %H:%M")
    except:
        return db_date.split()[0] if db_date else "no date"
    
def format_transaction_date(db_date):
    if not db_date:
        return "no date"
    try:
        dt = datetime.datetime.strptime(db_date, "%Y-%m-%d %H:%M:%S")
        return dt.strftime("%b %d")
    except:
        return db_date.split()[0] if db_date else "no date"
    
def get_user_currency(user_id):
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT default_currency FROM user_settings WHERE user_id = ?", (user_id,))
        currency = cursor.fetchone()
        return currency[0] if currency else 'USD'

def format_money(amount, currency='USD'):
    symbol = CURRENCIES.get(currency, '$')
    
    # European-style number formatting
    formatted_amount = "{:,.2f}".format(abs(amount)).replace(",", " ").replace(".", ",")
    
    # Handle currency symbol placement
    if currency in ['USD', 'GBP', 'JPY', 'RUB']:  # Prefix symbols
        return f"{symbol}{formatted_amount}"
    elif currency in ['EUR', 'CHF']:  # Suffix symbols
        return f"{formatted_amount}{symbol}"
    else:  # Default prefix
        return f"{symbol}{formatted_amount}"

def get_wallet_name(wallet_id):
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM wallets WHERE id = ?", (wallet_id,))
        wallet = cursor.fetchone()
        return wallet[0] if wallet else "Unknown Wallet"

def get_default_wallet(user_id):
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id FROM wallets WHERE user_id = ? AND is_default = 1",
            (user_id,)
        )
        wallet = cursor.fetchone()
        return wallet[0] if wallet else None
    
def convert_currency(amount, from_curr, to_curr):
    """Convert between currencies using Open Exchange Rates API"""
    if from_curr == to_curr:
        return amount
        
    try:
        response = requests.get(CURRENCY_API, timeout=5)
        response.raise_for_status()
        rates = response.json()['rates']
        
        # Convert via USD if needed
        if from_curr != 'USD':
            amount = amount / rates[from_curr]
        if to_curr != 'USD':
            amount = amount * rates[to_curr]
            
        return round(amount, 2)
    except Exception as e:
        logger.error(f"Currency conversion failed: {str(e)}")
        return amount  # Fallback if API fails

def get_monthly_summary(user_id, month, year):
    """Generate monthly financial summary"""
    conn = get_db_connection()
    start_date = f"{year}-{month:02d}-01"
    end_date = f"{year}-{month:02d}-{calendar.monthrange(year, month)[1]}"
    
    income = conn.execute(
        "SELECT SUM(amount) FROM transactions "
        "WHERE user_id = ? AND type = 'income' AND date BETWEEN ? AND ?",
        (user_id, start_date, end_date)
    ).fetchone()[0] or 0
    
    expenses = conn.execute(
        "SELECT SUM(amount) FROM transactions "
        "WHERE user_id = ? AND type = 'outcome' AND date BETWEEN ? AND ?",
        (user_id, start_date, end_date)
    ).fetchone()[0] or 0
    
    return {
        'income': income,
        'expenses': expenses,
        'savings': income + expenses  # expenses are negative
    }

def get_current_datetime():
    now = datetime.datetime.now(TIMEZONE)
    return now.strftime("%Y-%m-%d %H:%M:%S")

async def set_budget(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.message.from_user.id
    
    # Create category keyboard using CATEGORIES keys
    categories = list(CATEGORIES.keys())
    keyboard = [[InlineKeyboardButton(cat, callback_data=f"budgetcat_{cat}") 
               for cat in categories[i:i+2]] 
              for i in range(0, len(categories), 2)]
    keyboard.append([InlineKeyboardButton("🔙 Cancel", callback_data="cancel_budget")])
    
    await update.message.reply_text(
        "📊 Select a category for your budget:",
        reply_markup=InlineKeyboardMarkup(keyboard))
    return SET_BUDGET

async def budget_category_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle budget category selection"""
    query = update.callback_query
    await query.answer()
    category = query.data.split('_')[1]
    context.user_data['budget_category'] = category
    
    await query.edit_message_text(
        f"Setting budget for {category}.\n"
        f"Enter amount in {get_user_currency(query.from_user.id)}:")
    return SET_BUDGET

async def save_budget(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Save budget to database"""
    user_id = update.message.from_user.id
    try:
        amount = float(update.message.text)
        if amount <= 0:
            raise ValueError("Amount must be positive")
            
        category = context.user_data.get('budget_category')
        if not category:
            await update.message.reply_text(
                "Category not found. Please start over.",
                reply_markup=ReplyKeyboardMarkup(main_keyboard, resize_keyboard=True))
            return MAIN_MENU
            
        currency = get_user_currency(user_id)
        now = datetime.datetime.now(TIMEZONE)
        
        with get_db_connection() as conn:
            cursor = conn.cursor()
            # Get category ID
            cursor.execute(
                "SELECT id FROM categories WHERE name = ?", (category,))
            cat_row = cursor.fetchone()
            category_id = cat_row['id'] if cat_row else None
            
            if not category_id:
                await update.message.reply_text("Category not found in database")
                return SET_BUDGET
            
            # Delete any existing budget for this category/month/year
            cursor.execute(
                "DELETE FROM budgets WHERE user_id = ? AND category_id = ? "
                "AND month = ? AND year = ?",
                (user_id, category_id, now.month, now.year)
            )
            
            # Insert new budget
            cursor.execute(
                "INSERT INTO budgets (user_id, category_id, amount, currency, month, year) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (user_id, category_id, amount, currency, now.month, now.year)
            )
            conn.commit()
        
        await update.message.reply_text(
            f"✅ Budget set for {category}: {format_money(amount, currency)}",
            reply_markup=ReplyKeyboardMarkup(main_keyboard, resize_keyboard=True))
        return MAIN_MENU
        
    except ValueError as e:
        await update.message.reply_text(
            f"❌ Invalid amount: {str(e)}. Please enter a positive number.",
            reply_markup=ReplyKeyboardMarkup(cancel_keyboard, resize_keyboard=True))
        return SET_BUDGET
    

async def generate_report(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Generate financial reports"""
    keyboard = [[InlineKeyboardButton(rtype, callback_data=f"report_{rtype}")] 
               for rtype in REPORT_TYPES]
    keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="back")])
    
    await update.message.reply_text(
        "📈 Select report type:",
        reply_markup=InlineKeyboardMarkup(keyboard))
    return REPORT_MENU

async def show_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Display requested report"""
    query = update.callback_query
    await query.answer()
    report_type = query.data.split('_')[1]
    user_id = query.from_user.id
    now = datetime.datetime.now(TIMEZONE)
    currency = get_user_currency(user_id)
    
    if report_type == "Monthly Summary":
        summary = get_monthly_summary(user_id, now.month, now.year)
        text = (
            f"📅 Monthly Report ({now.strftime('%B %Y')})\n\n"
            f"📥 Income: {format_money(summary['income'], currency)}\n"
            f"📤 Expenses: {format_money(-summary['expenses'], currency)}\n"
            f"💾 Savings: {format_money(summary['savings'], currency)}\n"
            f"💸 Savings Rate: {summary['income'] and int(summary['savings']/summary['income']*100) or 0}%"
        )
        await query.edit_message_text(text)
    
    elif report_type == "Category Breakdown":
        # Generate category spending breakdown
        with get_db_connection() as conn:
            categories = conn.execute(
                "SELECT c.name, SUM(t.amount) as total "
                "FROM transactions t "
                "JOIN categories c ON t.category_id = c.id "
                "WHERE t.user_id = ? AND t.type = 'outcome' "
                "AND strftime('%Y-%m', t.date) = ? "
                "GROUP BY c.name",
                (user_id, f"{now.year}-{now.month:02d}")
            ).fetchall()
        
        if not categories:
            await query.edit_message_text("No categorized expenses this month!")
            return REPORT_MENU
            
        # Prepare pie chart
        labels = [cat['name'] for cat in categories]
        sizes = [-cat['total'] for cat in categories]  # expenses are negative
        
        plt.figure(figsize=(8, 6), facecolor='#F2F2F7')
        plt.pie(sizes, labels=labels, autopct='%1.1f%%', startangle=90, 
                colors=plt.cm.Pastel1.colors)
        plt.axis('equal')
        plt.title('Spending by Category', color='#1C1C1E')
        
        buf = BytesIO()
        plt.savefig(buf, format='png', dpi=120, transparent=True)
        buf.seek(0)
        plt.close()
        
        await query.message.reply_photo(
            photo=buf,
            caption="📊 Spending by Category",
            reply_markup=ReplyKeyboardMarkup(main_keyboard, resize_keyboard=True)
        )
    
    return MAIN_MENU

async def backup_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send user their data as JSON backup"""
    user_id = update.message.from_user.id
    
    with get_db_connection() as conn:
        # Fetch all user data
        transactions = conn.execute(
            "SELECT * FROM transactions WHERE user_id = ?", (user_id,)
        ).fetchall()
        holds = conn.execute(
            "SELECT * FROM holds WHERE user_id = ?", (user_id,)
        ).fetchall()
        recurring = conn.execute(
            "SELECT * FROM recurring WHERE user_id = ?", (user_id,)
        ).fetchall()
        budgets = conn.execute(
            "SELECT * FROM budgets WHERE user_id = ?", (user_id,)
        ).fetchall()
    
    # Convert to JSON
    data = {
        'transactions': [dict(ix) for ix in transactions],
        'holds': [dict(ix) for ix in holds],
        'recurring': [dict(ix) for ix in recurring],
        'budgets': [dict(ix) for ix in budgets]
    }
    
    # Send as file
    json_data = json.dumps(data, indent=2)
    await update.message.reply_document(
        document=BytesIO(json_data.encode()),
        filename=f"budget_backup_{datetime.date.today()}.json",
        caption="Here's your financial data backup 📦"
    )
    return MAIN_MENU

async def notify_budget_updates(context: ContextTypes.DEFAULT_TYPE):
    """Send weekly budget updates to users"""
    now = datetime.datetime.now(TIMEZONE)
    
    with get_db_connection() as conn:
        users = conn.execute("SELECT DISTINCT user_id FROM budgets").fetchall()
        
        for user in users:
            user_id = user['user_id']
            currency = get_user_currency(user_id)
            
            # Get monthly budget
            budgets = conn.execute(
                "SELECT c.name, b.amount, "
                "SUM(CASE WHEN t.type = 'outcome' THEN t.amount ELSE 0 END) as spent "
                "FROM budgets b "
                "JOIN categories c ON b.category_id = c.id "
                "LEFT JOIN transactions t ON t.category_id = c.id "
                "AND strftime('%Y-%m', t.date) = ? "
                "WHERE b.user_id = ? AND b.month = ? AND b.year = ? "
                "GROUP BY c.name, b.amount",
                (f"{now.year}-{now.month:02d}", user_id, now.month, now.year)
            ).fetchall()
            
            if not budgets:
                continue
                
            # Prepare notification
            message = "📋 Budget Update:\n\n"
            for budget in budgets:
                spent = -budget['spent'] if budget['spent'] else 0
                remaining = budget['amount'] - spent
                percentage = (spent / budget['amount']) * 100 if budget['amount'] else 0
                
                message += (
                    f"• {budget['name']}:\n"
                    f"  - Budget: {format_money(budget['amount'], currency)}\n"
                    f"  - Spent: {format_money(spent, currency)} ({percentage:.0f}%)\n"
                    f"  - Remaining: {format_money(remaining, currency)}\n\n"
                )
            
            try:
                await context.bot.send_message(
                    chat_id=user_id,
                    text=message
                )
            except Exception as e:
                logger.error(f"Failed to send notification to {user_id}: {str(e)}")


async def show_transactions_for_deletion(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.message.from_user.id
    currency = get_user_currency(user_id)
    
    with get_db_connection() as conn:
        transactions = conn.execute(
            "SELECT * FROM transactions WHERE user_id = ? "
            "ORDER BY date DESC LIMIT 50",
            (user_id,)
        ).fetchall()

    if not transactions:
        await update.message.reply_text(
            "No transactions found!",
            reply_markup=ReplyKeyboardMarkup(main_keyboard, resize_keyboard=True))
        return MAIN_MENU

    keyboard = []
    for idx, t in enumerate(transactions, 1):
        date = format_transaction_date(t['date'])
        trans_type = "Income" if t['type'] == 'income' else "Expense"
        amount = t['amount'] if t['amount'] > 0 else -t['amount']
        trans_currency = t['currency'] if 'currency' in t.keys() else currency
        description = t['description'][:20] + '...' if len(t['description']) > 20 else t['description']
        
        button_text = (
            f"{idx}. {date} | {trans_type} | "
            f"{format_money(amount, trans_currency)} | {description}"
        )
        keyboard.append([InlineKeyboardButton(button_text, callback_data=f"del_trans_{t['id']}")])

    keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="back_transactions")])

    await update.message.reply_text(
        "Select a transaction to delete:",
        reply_markup=InlineKeyboardMarkup(keyboard))
    return MANAGE_TRANSACTIONS

async def delete_transaction(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    trans_id = int(query.data.split('_')[2])

    with get_db_connection() as conn:
        conn.execute("DELETE FROM transactions WHERE id = ?", (trans_id,))
        conn.commit()

    await query.edit_message_text("✅ Transaction deleted successfully!")
    return await show_balance_menu(update, context)

async def process_recurring_transactions(context: ContextTypes.DEFAULT_TYPE):
    today = datetime.datetime.now(TIMEZONE).day
    with get_db_connection() as conn:
        recurring = conn.execute("SELECT * FROM recurring WHERE day_of_month = ? AND is_active = 1", 
                               (today,)).fetchall()
        
        for transaction in recurring:
            user_id = transaction['user_id']
            
            # Get default wallet
            wallet_id = get_default_wallet(user_id)
            if not wallet_id:
                logger.error(f"No default wallet for user {user_id}, skipping recurring transaction")
                continue
                
            cursor = conn.cursor()
            cursor.execute("SELECT currency FROM wallets WHERE id = ?", (wallet_id,))
            wallet_currency = cursor.fetchone()[0]
            
            # Convert currency if needed
            amount = transaction['amount']
            if transaction['currency'] != wallet_currency:
                converted_amount = convert_currency(amount, transaction['currency'], wallet_currency)
                currency = wallet_currency
            else:
                converted_amount = amount
                currency = transaction['currency']
            
            # Handle outcome type
            if transaction['type'] == 'outcome':
                converted_amount = -converted_amount
            
            # Insert transaction
            cursor.execute(
                "INSERT INTO transactions (user_id, type, amount, description, date, currency, wallet_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (user_id, transaction['type'], converted_amount, 
                 transaction['description'], get_current_datetime(), currency, wallet_id)
            )
            
            # Update wallet balance
            cursor.execute(
                "UPDATE wallets SET balance = balance + ? WHERE id = ?",
                (converted_amount, wallet_id)
            )
            conn.commit()
            
            logger.info(f"Processed recurring transaction for user {user_id}")

async def show_balance_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Display balance and return to main menu"""
    await show_balance(update, context)
    return MAIN_MENU

async def show_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message:
        user_id = update.message.from_user.id
    elif update.callback_query:
        user_id = update.callback_query.from_user.id
    else:
        return MAIN_MENU
        
    currency = get_user_currency(user_id)
    now = datetime.datetime.now(TIMEZONE)
    currency_symbol = CURRENCIES.get(currency, '')
    
    with get_db_connection() as conn:
        cursor = conn.cursor()
        
        # Get all wallets with detailed info
        cursor.execute("SELECT * FROM wallets WHERE user_id = ?", (user_id,))
        wallets = cursor.fetchall()
        
        if not wallets:
            if update.message:
                await update.message.reply_text(
                    "No wallets found. Please add a wallet first!",
                    reply_markup=ReplyKeyboardMarkup(main_keyboard, resize_keyboard=True))
            return MAIN_MENU
        
        # Prepare wallet details
        wallet_details = []
        total_balance = 0
        for wallet in wallets:
            wallet_balance = wallet['balance']
            wallet_currency = wallet['currency']
            wallet_symbol = CURRENCIES.get(wallet_currency, '')
            converted_balance = convert_currency(wallet_balance, wallet_currency, currency)
            total_balance += converted_balance
            
            # Choose icon based on wallet name
            wallet_icon = "🏦" if "bank" in wallet['name'].lower() else "💵"
            wallet_icon = "🏦" if "post" in wallet['name'].lower() else wallet_icon
            wallet_icon = "💳" if "card" in wallet['name'].lower() else wallet_icon
            wallet_icon = "💰" if "cash" in wallet['name'].lower() else wallet_icon
            
            # Format with right alignment
            balance_str = f"{format_money(wallet_balance, wallet_currency)}"
            wallet_details.append(
                f"{wallet_icon} {wallet['name']}:"
                f"{balance_str:>20} "
                f"{'✅' if wallet['is_default'] else ''}"
            )

        # Get financial data
        cursor.execute("SELECT SUM(amount) FROM transactions WHERE user_id = ? AND type = 'income'", (user_id,))
        income = cursor.fetchone()[0] or 0
        
        cursor.execute("SELECT SUM(amount) FROM transactions WHERE user_id = ? AND type = 'outcome'", (user_id,))
        outcome = cursor.fetchone()[0] or 0
        
        cursor.execute("SELECT SUM(amount) FROM holds WHERE user_id = ?", (user_id,))
        holds = cursor.fetchone()[0] or 0
        
        balance = income + outcome
        total_expenses = -outcome if outcome < 0 else outcome

        # Get monthly spending by category with icons
        category_icons = {
            'Food': '🍔',
            'Transport': '🚖',
            'Housing': '🏠',
            'Entertainment': '🎮',
            'Healthcare': '⚕️',
            'Other': '📦'
        }
        
        cursor.execute(
            """SELECT c.name, SUM(t.amount) as total 
               FROM transactions t 
               JOIN categories c ON t.category_id = c.id 
               WHERE t.user_id = ? AND t.type = 'outcome' 
               AND strftime('%Y-%m', t.date) = ? 
               GROUP BY c.name""",
            (user_id, now.strftime("%Y-%m")))
        spending_by_category = cursor.fetchall()

    # Calculate metrics
    savings_rate = (income + outcome) / income * 100 if income else 0
    avg_daily_spending = total_expenses / now.day if now.day else 0
    
    # Create progress bar for savings rate
    def progress_bar(percentage):
        filled = '▓' * int(percentage / 10)
        empty = '░' * (10 - len(filled))
        return f"{filled}{empty}"
    
    # Format all monetary values consistently
    def fm(amount):
        return format_money(amount, currency)
    
    # Create formatted text with wallet details
    text_summary = (
        f"💠 <b>𝗙𝗶𝗻𝗮𝗻𝗰𝗶𝗮𝗹 𝗦𝘂𝗺𝗺𝗮𝗿𝘆</b> – {now.strftime('%B %Y')}\n\n"
        
        f"👛 <b>𝗪𝗮𝗹𝗹𝗲𝘁 𝗕𝗮𝗹𝗮𝗻𝗰𝗲𝘀</b>\n" + "\n".join(wallet_details) + "\n\n"
        
        f"<b>💳 𝗧𝗼𝘁𝗮𝗹 𝗕𝗮𝗹𝗮𝗻𝗰𝗲:</b> {fm(total_balance):>16}\n"
        #f"<b>🌍 𝗖𝘂𝗿𝗿𝗲𝗻𝗰𝘆:</b> {currency} {currency_symbol:>22}\n\n"
        
        "───────────────\n\n"
        
        f"<b>📈 𝗜𝗻𝗰𝗼𝗺𝗲:</b>           +{fm(income)}\n"
        f"<b>📉 𝗘𝘅𝗽𝗲𝗻𝘀𝗲𝘀:</b>          –{fm(total_expenses)}\n"
        f"<b>⏳ 𝗛𝗲𝗹𝗱 𝗔𝗺𝗼𝘂𝗻𝘁:</b>      {fm(holds)}\n\n"
        
        #f"<b>📊 𝗠𝗲𝘁𝗿𝗶𝗰𝘀</b>\n"
        f"💸 <b>Savings Rate:</b>     {savings_rate:.1f}%\n"
        f"🗓️ <b>Avg Daily Spend:</b>  {fm(avg_daily_spending)}\n\n"
    )
    
    # Add spending by category if available
    if spending_by_category:
        text_summary += f"<b>📋 𝗦𝗽𝗲𝗻𝗱𝗶𝗻𝗴 𝗕𝘆 𝗖𝗮𝘁𝗲𝗴𝗼𝗿𝘆</b>\n"
        for category in spending_by_category:
            category_name = category['name']
            category_total = -category['total']  # expenses are negative
            icon = category_icons.get(category_name, '•')
            text_summary += f"{icon} {category_name}: {fm(category_total):>15}\n"
        text_summary += "\n"
    
    # Add financial health indicator
    financial_health = "✅ Excellent" if balance > 0 else "⚠️ Breaking Even" if balance == 0 else "❌ Over Budget"
    text_summary += f"<b>{financial_health} 𝗙𝗶𝗻𝗮𝗻𝗰𝗶𝗮𝗹 𝗛𝗲𝗮𝗹𝘁𝗵</b>"

    # Create visualization
    plt.style.use('default')
    fig, ax = plt.subplots(figsize=(8, 6), facecolor='#F2F2F7')
    fig.patch.set_alpha(0)
    
    ios_colors = {
        'income': '#32D74B',
        'expenses': '#FF453A',
        'holds': '#FF9F0A',
        'background': '#F2F2F7',
        'text': '#1C1C1E'
    }
    
    for spine in ax.spines.values():
        spine.set_visible(False)
    ax.grid(False)
    
    sizes = [income, total_expenses, holds]
    labels = [f'Income\n{fm(income)}', 
              f'Expenses\n{fm(total_expenses)}', 
              f'Holds\n{fm(holds)}']
    colors = [ios_colors['income'], ios_colors['expenses'], ios_colors['holds']]
    
    wedges, texts = ax.pie(sizes, colors=colors, startangle=90, 
                          wedgeprops=dict(width=0.5, edgecolor='none'))
    
    for text in texts:
        text.set_color(ios_colors['text'])
        text.set_fontsize(10)
        text.set_fontweight('medium')
    
    center_text = f"Balance\n{fm(balance)}"
    ax.text(0, 0, center_text, ha='center', va='center', 
           fontsize=18, fontweight='bold', color=ios_colors['text'])
    
    for wedge in wedges:
        wedge.set_edgecolor('#D1D1D6')
        wedge.set_linewidth(0.5)
    
    plt.tight_layout()
    
    buf = BytesIO()
    plt.savefig(buf, format='png', dpi=120, transparent=True, 
               bbox_inches='tight', pad_inches=0.1)
    buf.seek(0)
    plt.close(fig)
    
    # Create keyboard with "Show Recent Transactions" button
    keyboard = [[InlineKeyboardButton("📝 Show Recent Transactions", callback_data="show_recent_trans")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    # Send the message
    if update.message:
        await update.message.reply_photo(
            photo=buf,
            caption=text_summary,
            parse_mode='HTML',
            reply_markup=reply_markup
        )
    elif update.callback_query:
        await update.callback_query.message.reply_photo(
            photo=buf,
            caption=text_summary,
            parse_mode='HTML',
            reply_markup=reply_markup
        )

    return MAIN_MENU

async def update_code1(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != 676453411:  # Replace with your actual Telegram ID
        await update.message.reply_text("⛔️ You don't have permission to execute this command.")
        return

    await update.message.reply_text("🔄 Updating code...")

    try:
        git_result = subprocess.run(
            ["git", "pull"],
            cwd="/root/bot",
            capture_output=True,
            text=True,
            check=True
        )

        subprocess.run(
            ["systemctl", "restart", "bot"],
            check=True
        )

        await update.message.reply_text(f"✅ Done!\n\n<code>{git_result.stdout.strip()}</code>", parse_mode="HTML")

    except subprocess.CalledProcessError as e:
        await update.message.reply_text(f"❌ Error:\n<code>{e.stderr}</code>", parse_mode="HTML")

async def wallets_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.message.from_user.id
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM wallets WHERE user_id = ?", (user_id,))
        wallets = cursor.fetchall()
    
    if not wallets:
        text = "You don't have any wallets yet. Add your first wallet!"
        keyboard = [['➕ Add Wallet'], ['🔙 Back']]
    else:
        text = "👛 Your Wallets:\n"
        for wallet in wallets:
            text += f"• {wallet['name']}: {format_money(wallet['balance'], wallet['currency'])}"
            if wallet['is_default']:
                text += " (Default)"
            text += "\n"
        keyboard = [
            ['➕ Add Wallet'],
            ['🏷 Set Default Wallet'],
            ['💸 Transfer Funds'],
            ['🔙 Back']
        ]
    
    await update.message.reply_text(
        text,
        reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True))
    return WALLET_MENU

async def add_wallet_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "Enter wallet details in format:\n"
        "<b>Name Currency</b>\n"
        "Example: Cash USD\n"
        "Available currencies: USD, EUR, CHF, GBP, JPY, RUB",
        reply_markup=ReplyKeyboardMarkup(cancel_keyboard, resize_keyboard=True),
        parse_mode="HTML")
    return ADD_WALLET

async def add_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.message.from_user.id
    text = update.message.text.split()
    
    if len(text) < 2:
        await update.message.reply_text("Invalid format. Please enter: Name Currency")
        return ADD_WALLET
    
    name = ' '.join(text[:-1])
    currency = text[-1].upper()
    
    if currency not in CURRENCIES:
        await update.message.reply_text(
            f"❌ Invalid currency. Available: {', '.join(CURRENCIES.keys())}")
        return ADD_WALLET
    
    with get_db_connection() as conn:
        cursor = conn.cursor()
        # Set as default if first wallet
        cursor.execute("SELECT COUNT(*) FROM wallets WHERE user_id = ?", (user_id,))
        count = cursor.fetchone()[0]
        is_default = 1 if count == 0 else 0
        
        cursor.execute(
            "INSERT INTO wallets (user_id, name, currency, is_default) VALUES (?, ?, ?, ?)",
            (user_id, name, currency, is_default)
        )
        conn.commit()
    
    await update.message.reply_text(
        f"✅ Wallet added: {name} ({currency})",
        reply_markup=ReplyKeyboardMarkup(main_keyboard, resize_keyboard=True))
    return MAIN_MENU

async def set_default_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.message.from_user.id
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM wallets WHERE user_id = ?", (user_id,))
        wallets = cursor.fetchall()
    
    if not wallets:
        await update.message.reply_text("No wallets available. Add a wallet first!")
        return await wallets_menu(update, context)
    
    keyboard = [
        [InlineKeyboardButton(f"{wallet['name']} ({wallet['currency']})", 
         callback_data=f"setdef_{wallet['id']}")]
        for wallet in wallets
    ]
    keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="back_wallets")])
    
    await update.message.reply_text(
        "Select a wallet to set as default:",
        reply_markup=InlineKeyboardMarkup(keyboard))
    return WALLET_MENU

async def handle_set_default(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    
    # Extract wallet ID from callback data
    try:
        wallet_id = int(query.data.split('_')[1])
    except (IndexError, ValueError):
        logger.error(f"Invalid callback data: {query.data}")
        await query.edit_message_text("❌ Invalid wallet selection")
        return await wallets_menu(update, context)
    
    user_id = query.from_user.id
    
    with get_db_connection() as conn:
        cursor = conn.cursor()
        try:
            # Clear previous default
            cursor.execute(
                "UPDATE wallets SET is_default = 0 WHERE user_id = ?",
                (user_id,))
            
            # Set new default
            cursor.execute(
                "UPDATE wallets SET is_default = 1 WHERE id = ? AND user_id = ?",
                (wallet_id, user_id))
            
            # Verify update
            cursor.execute(
                "SELECT name, currency FROM wallets WHERE id = ? AND user_id = ?",
                (wallet_id, user_id))
            wallet = cursor.fetchone()
            
            if not wallet:
                await query.edit_message_text("❌ Wallet not found")
                return await wallets_menu(update, context)
            
            conn.commit()
            
            await query.edit_message_text(
                f"✅ Default wallet set to: {wallet['name']} ({wallet['currency']})",
                reply_markup=ReplyKeyboardMarkup(main_keyboard, resize_keyboard=True))
            
        except sqlite3.Error as e:
            conn.rollback()
            logger.error(f"Database error setting default wallet: {e}")
            await query.edit_message_text("❌ Error updating wallet")
            return await wallets_menu(update, context)
    
    return await wallets_menu(update, context)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.message.from_user
    
    with get_db_connection() as conn:
        user_exists = conn.execute("SELECT 1 FROM user_settings WHERE user_id = ?", 
                                 (user.id,)).fetchone()
        if not user_exists:
            conn.execute("INSERT INTO user_settings (user_id, default_currency) VALUES (?, ?)",
                       (user.id, 'USD'))
            conn.commit()
    
    await update.message.reply_text(
        f"Welcome to Budget Planner, {user.first_name}!\n"
        "Use the buttons below to manage your finances:",
        reply_markup=ReplyKeyboardMarkup(main_keyboard, resize_keyboard=True))
    return MAIN_MENU

async def currency_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    keyboard = []
    currencies = list(CURRENCIES.items())
    for i in range(0, len(currencies), 2):
        row = []
        for code, symbol in currencies[i:i+2]:
            row.append(InlineKeyboardButton(f"{code} {symbol}", callback_data=f"currency_{code}"))
        keyboard.append(row)
    keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="back_currency")])
    
    await update.message.reply_text(
        "Select your default currency:",
        reply_markup=InlineKeyboardMarkup(keyboard))
    return CURRENCY_MENU

async def set_currency(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    currency_code = query.data.split('_')[1]
    user_id = query.from_user.id
    
    with get_db_connection() as conn:
        conn.execute("INSERT OR REPLACE INTO user_settings (user_id, default_currency) VALUES (?, ?)",
                   (user_id, currency_code))
        conn.commit()
    
    await query.edit_message_text(
        f"✅ Default currency set to {currency_code} {CURRENCIES.get(currency_code, '')}")
    return await show_balance_menu(update, context)

async def recurring_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    keyboard = [
        ["➕ Add Recurring", "📋 List Recurring"],
        ["🔙 Back"]
    ]
    await update.message.reply_text(
        "Manage recurring transactions:",
        reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True))
    return RECURRING_MENU

async def add_recurring_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    keyboard = [["Income", "Outcome"], ["❌ Cancel"]]
    await update.message.reply_text(
        "Select type of recurring transaction:",
        reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True))
    return ADD_RECURRING

async def add_recurring_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['recurring_type'] = update.message.text.lower()
    await update.message.reply_text(
        "Enter details in format:\n"
        "<b>Amount DayOfMonth Description</b>\n"
        "Example: 1000 15 Salary\n"
        "DayOfMonth should be between 1-31",
        reply_markup=ReplyKeyboardMarkup(cancel_keyboard, resize_keyboard=True),
        parse_mode="HTML")
    return ADD_RECURRING

async def add_recurring(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.message.from_user.id
    text = update.message.text.split()
    currency = get_user_currency(user_id)

    try:
        if 'recurring_type' not in context.user_data:
            await update.message.reply_text(
                "Please start over and select the transaction type first",
                reply_markup=ReplyKeyboardMarkup(cancel_keyboard, resize_keyboard=True))
            return ADD_RECURRING

        amount = float(text[0])
        day = int(text[1])
        description = ' '.join(text[2:]) if len(text) > 2 else "Recurring"
        
        if not 1 <= day <= 31:
            raise ValueError("Day must be between 1-31")

        with get_db_connection() as conn:
            conn.execute(
                "INSERT INTO recurring (user_id, type, amount, description, day_of_month, currency) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (user_id, context.user_data['recurring_type'], amount, description, day, currency)
            )
            conn.commit()

        context.user_data.pop('recurring_type', None)
        await update.message.reply_text(
            "✅ Recurring transaction added!",
            reply_markup=ReplyKeyboardMarkup(main_keyboard, resize_keyboard=True))
        return MAIN_MENU

    except (ValueError, IndexError) as e:
        await update.message.reply_text(
            f"❌ Invalid format: {str(e)}\n"
            "Please enter: Amount DayOfMonth Description\n"
            "Example: 1000 15 Salary",
            reply_markup=ReplyKeyboardMarkup(cancel_keyboard, resize_keyboard=True))
        return ADD_RECURRING

async def list_recurring(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.message.from_user.id
    
    with get_db_connection() as conn:
        recurring = conn.execute("SELECT * FROM recurring WHERE user_id = ?", 
                              (user_id,)).fetchall()

    if not recurring:
        await update.message.reply_text(
            "No recurring transactions found!",
            reply_markup=ReplyKeyboardMarkup(main_keyboard, resize_keyboard=True))
        return MAIN_MENU

    keyboard = []
    for idx, trans in enumerate(recurring, 1):
        button_text = (
            f"{idx}. {trans['type'].capitalize()}: "
            f"{format_money(trans['amount'], trans['currency'])} "
            f"on day {trans['day_of_month']} - {trans['description']}"
        )
        keyboard.append([InlineKeyboardButton(button_text, callback_data=f"recur_{trans['id']}")])
    keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="back_recurring")])

    await update.message.reply_text(
        "📋 Your recurring transactions:\nSelect one to manage:",
        reply_markup=InlineKeyboardMarkup(keyboard))
    return RECURRING_MENU

async def manage_recurring(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    recurring_id = int(query.data.split('_')[1])
    context.user_data['current_recurring'] = recurring_id

    with get_db_connection() as conn:
        recurring = conn.execute("SELECT * FROM recurring WHERE id = ?", 
                              (recurring_id,)).fetchone()

    currency = recurring['currency']
    
    await query.edit_message_text(
        text=f"Recurring transaction selected:\n"
             f"Type: {recurring['type'].capitalize()}\n"
             f"Amount: {format_money(recurring['amount'], currency)}\n"
             f"Day: {recurring['day_of_month']}\n"
             f"Description: {recurring['description']}\n"
             f"Currency: {currency} {CURRENCIES.get(currency, '')}\n\n"
             "Choose action:",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✏️ Edit", callback_data=f"edit_recur_{recurring_id}"),
             InlineKeyboardButton("❌ Remove", callback_data=f"remove_recur_{recurring_id}")],
            [InlineKeyboardButton("🔙 Back", callback_data="back_recur_list")]
        ]))
    return RECURRING_MENU

async def remove_recurring(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    recurring_id = int(query.data.split('_')[2])

    with get_db_connection() as conn:
        conn.execute("DELETE FROM recurring WHERE id = ?", (recurring_id,))
        conn.commit()

    await query.edit_message_text("✅ Recurring transaction removed successfully!")
    return await show_balance_menu(update, context)

async def edit_recurring_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    recurring_id = int(query.data.split('_')[2])
    context.user_data['editing_recurring'] = recurring_id

    with get_db_connection() as conn:
        recurring = conn.execute("SELECT * FROM recurring WHERE id = ?", 
                              (recurring_id,)).fetchone()
    
    currency = recurring['currency']
    
    await query.edit_message_text(
        f"Editing recurring transaction:\n"
        f"Current: {format_money(recurring['amount'], currency)} on day {recurring['day_of_month']} - {recurring['description']}\n\n"
        "Enter new details in format:\n"
        "<b>Amount DayOfMonth Description</b>\n"
        "Example: 1000 15 Salary\n"
        "DayOfMonth should be between 1-31",
        parse_mode="HTML"
    )
    return ADD_RECURRING

async def edit_recurring(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.message.from_user.id
    text = update.message.text.split()

    try:
        amount = float(text[0])
        day = int(text[1])
        description = ' '.join(text[2:]) if len(text) > 2 else "Recurring"
        
        if not 1 <= day <= 31:
            raise ValueError("Day must be between 1-31")

        with get_db_connection() as conn:
            conn.execute(
                "UPDATE recurring SET amount = ?, day_of_month = ?, description = ? WHERE id = ?",
                (amount, day, description, context.user_data['editing_recurring'])
            )
            conn.commit()

        context.user_data.pop('editing_recurring', None)
        await update.message.reply_text("✅ Recurring transaction updated successfully!")
        return await show_balance_menu(update, context)

    except (ValueError, IndexError) as e:
        await update.message.reply_text(
            f"❌ Invalid format: {str(e)}\n"
            "Please enter: Amount DayOfMonth Description\n"
            "Example: 1000 15 Salary",
            reply_markup=ReplyKeyboardMarkup(cancel_keyboard, resize_keyboard=True))
        return ADD_RECURRING

async def income_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.message.from_user.id
    currency = get_user_currency(user_id)
    
    await update.message.reply_text(
        f"➕ Add income in format (currency: {currency}):\n"
        "<b>Amount</b> (e.g., 1000)\n"
        "<b>Amount Currency</b> (e.g., 1000 EUR)\n"
        "<b>Amount Currency Description</b> (e.g., 1000 EUR Salary)",
        reply_markup=ReplyKeyboardMarkup(cancel_keyboard, resize_keyboard=True),
        parse_mode="HTML")
    return ADD_INCOME

async def add_income(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.message.from_user.id
    text = update.message.text.split()
    default_currency = get_user_currency(user_id)
    
    try:
        # Parse input
        if len(text) > 1 and text[1].upper() in CURRENCIES:
            amount = float(text[0])
            currency = text[1].upper()
            description = ' '.join(text[2:]) if len(text) > 2 else "Income"
        else:
            amount = float(text[0])
            currency = default_currency
            description = ' '.join(text[1:]) if len(text) > 1 else "Income"
        
        # Get default wallet
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT id, currency FROM wallets WHERE user_id = ? AND is_default = 1",
                (user_id,)
            )
            wallet = cursor.fetchone()
            
            if not wallet:
                await update.message.reply_text(
                    "❌ No default wallet set. Please create a wallet first.",
                    reply_markup=ReplyKeyboardMarkup(main_keyboard, resize_keyboard=True))
                return MAIN_MENU
                
            wallet_id = wallet[0]
            wallet_currency = wallet[1]
            
            # Convert amount to wallet currency if needed
            if currency != wallet_currency:
                converted_amount = convert_currency(amount, currency, wallet_currency)
                currency = wallet_currency
                amount = converted_amount
            
            # Insert transaction
            cursor.execute(
                "INSERT INTO transactions (user_id, type, amount, description, date, currency, category_id, wallet_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (user_id, 'income', amount, description, get_current_datetime(), currency, None, wallet_id)
            )
            
            # Update wallet balance
            cursor.execute(
                "UPDATE wallets SET balance = balance + ? WHERE id = ?",
                (amount, wallet_id)
            )
            conn.commit()
        
        await update.message.reply_text(
            f"✅ Income recorded: {format_money(amount, currency)}\n"
            f"📝 Description: {description}\n"
            f"👛 Wallet: {get_wallet_name(wallet_id)}",
            reply_markup=ReplyKeyboardMarkup(main_keyboard, resize_keyboard=True))
        
        return MAIN_MENU

    except (ValueError, IndexError):
        await update.message.reply_text(
            "❌ Invalid format. Please enter: Amount [Currency] [Description]",
            reply_markup=ReplyKeyboardMarkup(cancel_keyboard, resize_keyboard=True))
        return ADD_INCOME


async def outcome_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.message.from_user.id
    currency = get_user_currency(user_id)
    
    await update.message.reply_text(
        f"➖ Add expense in format (currency: {currency}):\n"
        "<b>Amount</b> (e.g., 50)\n"
        "<b>Amount Description</b> (e.g., 50 Groceries)\n"
        "<b>Amount Currency Description</b> (e.g., 50 EUR Dinner)",
        reply_markup=ReplyKeyboardMarkup(cancel_keyboard, resize_keyboard=True),
        parse_mode="HTML")
    return ADD_OUTCOME

async def add_outcome(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.message.from_user.id
    text = update.message.text.split()
    default_currency = get_user_currency(user_id)
    
    try:
        # Parse currency if provided
        if len(text) > 1 and text[1].upper() in CURRENCIES:
            amount = float(text[0])
            currency = text[1].upper()
            description = ' '.join(text[2:]) if len(text) > 2 else "Expense"
        else:
            amount = float(text[0])
            currency = default_currency
            description = ' '.join(text[1:]) if len(text) > 1 else "Expense"
        
        # Auto-detect category based on description
        category = detect_category(description)
        
        with get_db_connection() as conn:
            cursor = conn.cursor()
            
            # Get default wallet
            wallet_id = get_default_wallet(user_id)
            if not wallet_id:
                await update.message.reply_text(
                    "❌ No default wallet set. Please create a wallet first.",
                    reply_markup=ReplyKeyboardMarkup(main_keyboard, resize_keyboard=True))
                return MAIN_MENU
                
            cursor.execute("SELECT currency FROM wallets WHERE id = ?", (wallet_id,))
            wallet_currency = cursor.fetchone()[0]
            
            # Convert amount to wallet currency if needed
            if currency != wallet_currency:
                converted_amount = convert_currency(amount, currency, wallet_currency)
                amount = converted_amount
                currency = wallet_currency
            
            # Get or create category
            cursor.execute(
                "SELECT id FROM categories WHERE name = ?", (category,))
            cat_row = cursor.fetchone()
            
            if not cat_row:
                cursor.execute(
                    "INSERT INTO categories (name) VALUES (?)", (category,))
                category_id = cursor.lastrowid
            else:
                category_id = cat_row[0]
            
            # Insert transaction
            cursor.execute(
                "INSERT INTO transactions (user_id, type, amount, description, date, currency, category_id, wallet_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (user_id, 'outcome', -amount, description, get_current_datetime(), currency, category_id, wallet_id)
            )
            
            # Update wallet balance
            cursor.execute(
                "UPDATE wallets SET balance = balance - ? WHERE id = ?",
                (amount, wallet_id)
            )
            conn.commit()
        
        # Show confirmation with detected category
        await update.message.reply_text(
            f"✅ Expense recorded: {format_money(amount, currency)}\n"
            f"📝 Description: {description}\n"
            f"🏷 Category: {category}\n"
            f"👛 Wallet: {get_wallet_name(wallet_id)}",
            reply_markup=ReplyKeyboardMarkup(main_keyboard, resize_keyboard=True))
        
        return MAIN_MENU

    except (ValueError, IndexError):
        await update.message.reply_text(
            "❌ Invalid format. Please enter: Amount [Currency] [Description]",
            reply_markup=ReplyKeyboardMarkup(cancel_keyboard, resize_keyboard=True))
        return ADD_OUTCOME

async def holds_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.message.from_user.id
    currency = get_user_currency(user_id)
    
    with get_db_connection() as conn:
        holds = conn.execute("SELECT * FROM holds WHERE user_id = ?", (user_id,)).fetchall()

    if not holds:
        text = f"No holds found! Add a new hold (currency: {currency}):"
        keyboard = [['➕ Add Hold'], ['🔙 Back']]
    else:
        holds_list = "\n".join(
            f"{idx+1}. {format_money(hold['amount'], hold['currency'])} - {hold['description']}" 
            for idx, hold in enumerate(holds)
        )
        text = f"⏳ Your holds:\n{holds_list}\n\nSelect an action:"
        keyboard = [['➕ Add Hold'], ['🛠 Manage Hold'], ['🔙 Back']]
    
    await update.message.reply_text(
        text,
        reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True))
    
    # Return the correct state based on the menu we're showing
    if not holds:
        return ADD_HOLD
    return MANAGE_HOLDS

async def add_hold_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.message.from_user.id
    currency = get_user_currency(user_id)
    await update.message.reply_text(
        f"⏳ Add hold in format (currency: {currency}):\n"
        "<b>Amount</b> (e.g., 1000)\n"
        "OR\n"
        "<b>Amount Description</b> (e.g., 1000 Amazon)",
        reply_markup=ReplyKeyboardMarkup(cancel_keyboard, resize_keyboard=True),
        parse_mode="HTML")
    return ADD_HOLD

async def add_hold(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.message.from_user.id
    text = update.message.text.split()
    currency = get_user_currency(user_id)

    try:
        amount = float(text[0])
        description = ' '.join(text[1:]) if len(text) > 1 else "Hold"

        with get_db_connection() as conn:
            conn.execute(
                "INSERT INTO holds (user_id, amount, description, currency) VALUES (?, ?, ?, ?)",
                (user_id, amount, description, currency)
            )
            conn.commit()

        await update.message.reply_text(
            f"✅ Hold added: {format_money(amount, currency)} - {description}",
            reply_markup=ReplyKeyboardMarkup(main_keyboard, resize_keyboard=True))
        return MAIN_MENU

    except (ValueError, IndexError):
        await update.message.reply_text(
            "❌ Invalid format. Please enter amount and optional description\n"
            "Example: 500 Vacation savings",
            reply_markup=ReplyKeyboardMarkup(cancel_keyboard, resize_keyboard=True))
        return ADD_HOLD

async def transfer_funds_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Prompt user to select source wallet for transfer"""
    user_id = update.message.from_user.id
    
    with get_db_connection() as conn:
        wallets = conn.execute(
            "SELECT * FROM wallets WHERE user_id = ? AND balance > 0",
            (user_id,)
        ).fetchall()

    if len(wallets) < 2:
        await update.message.reply_text(
            "❌ You need at least 2 wallets with funds to transfer",
            reply_markup=ReplyKeyboardMarkup(main_keyboard, resize_keyboard=True))
        return MAIN_MENU

    keyboard = [
        [InlineKeyboardButton(
            f"{wallet['name']} ({format_money(wallet['balance'], wallet['currency'])})", 
            callback_data=f"transfer_from_{wallet['id']}"
        )]
        for wallet in wallets
    ]
    keyboard.append([InlineKeyboardButton("🔙 Cancel", callback_data="cancel_transfer")])

    await update.message.reply_text(
        "💸 Select source wallet for transfer:",
        reply_markup=InlineKeyboardMarkup(keyboard))
    return WALLET_MENU

async def select_target_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle source wallet selection and prompt for target wallet"""
    query = update.callback_query
    await query.answer()
    source_wallet_id = int(query.data.split('_')[2])
    context.user_data['transfer'] = {'from': source_wallet_id}
    
    user_id = query.from_user.id
    
    with get_db_connection() as conn:
        # Get source wallet details
        source_wallet = conn.execute(
            "SELECT * FROM wallets WHERE id = ?", 
            (source_wallet_id,)
        ).fetchone()
        
        # Get available target wallets (excluding source)
        target_wallets = conn.execute(
            "SELECT * FROM wallets WHERE user_id = ? AND id != ?",
            (user_id, source_wallet_id)
        ).fetchall()

    keyboard = [
        [InlineKeyboardButton(
            f"{wallet['name']} ({wallet['currency']})", 
            callback_data=f"transfer_to_{wallet['id']}"
        )]
        for wallet in target_wallets
    ]
    keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="back_transfer")])

    await query.edit_message_text(
        f"Transferring from: {source_wallet['name']} ({source_wallet['currency']})\n"
        "Select target wallet:",
        reply_markup=InlineKeyboardMarkup(keyboard))
    return WALLET_MENU

async def enter_transfer_amount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle target wallet selection and prompt for amount"""
    query = update.callback_query
    await query.answer()
    target_wallet_id = int(query.data.split('_')[2])
    context.user_data['transfer']['to'] = target_wallet_id
    
    with get_db_connection() as conn:
        # Get wallet details
        source_wallet = conn.execute(
            "SELECT * FROM wallets WHERE id = ?",
            (context.user_data['transfer']['from'],)
        ).fetchone()
        
        target_wallet = conn.execute(
            "SELECT * FROM wallets WHERE id = ?",
            (target_wallet_id,)
        ).fetchone()

    context.user_data['transfer']['from_currency'] = source_wallet['currency']
    context.user_data['transfer']['to_currency'] = target_wallet['currency']
    
    # Edit the message to prompt for amount
    await query.edit_message_text(
        f"💸 Transfer from {source_wallet['name']} ({source_wallet['currency']}) "
        f"to {target_wallet['name']} ({target_wallet['currency']})\n\n"
        f"Available: {format_money(source_wallet['balance'], source_wallet['currency'])}\n"
        "Enter amount to transfer:",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Cancel", callback_data="cancel_transfer")]]))
    
    # Return the state that will process the amount input
    return WALLET_MENU

async def process_transfer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Process the wallet-to-wallet transfer"""
    user_id = update.message.from_user.id
    amount_text = update.message.text.replace(',', '.')  # Handle both decimal separators
    
    try:
        amount = float(amount_text)
        if amount <= 0:
            raise ValueError("Amount must be positive")
            
        transfer_data = context.user_data.get('transfer')
        if not transfer_data:
            await update.message.reply_text(
                "Transfer data missing. Please start over.",
                reply_markup=ReplyKeyboardMarkup(main_keyboard, resize_keyboard=True))
            return MAIN_MENU
            
        from_wallet = transfer_data['from']
        to_wallet = transfer_data['to']
        from_currency = transfer_data['from_currency']
        to_currency = transfer_data['to_currency']
        
        with get_db_connection() as conn:
            # Verify source wallet has sufficient balance
            cursor = conn.cursor()
            cursor.execute(
                "SELECT balance FROM wallets WHERE id = ? AND user_id = ?",
                (from_wallet, user_id)
            )
            wallet = cursor.fetchone()
            
            if not wallet:
                raise ValueError("Source wallet not found")
                
            source_balance = wallet['balance']
            
            if amount > source_balance:
                raise ValueError(f"Insufficient funds. Available: {format_money(source_balance, from_currency)}")
            
            # Convert currency if needed
            if from_currency != to_currency:
                converted_amount = convert_currency(amount, from_currency, to_currency)
            else:
                converted_amount = amount
            
            # Perform the transfer
            # Deduct from source wallet
            cursor.execute(
                "UPDATE wallets SET balance = balance - ? WHERE id = ?",
                (amount, from_wallet)
            )
            
            # Add to target wallet
            cursor.execute(
                "UPDATE wallets SET balance = balance + ? WHERE id = ?",
                (converted_amount, to_wallet)
            )
            
            # Record transaction history
            from_name = get_wallet_name(from_wallet)
            to_name = get_wallet_name(to_wallet)
            
            # Source wallet transaction
            cursor.execute(
                "INSERT INTO transactions (user_id, type, amount, description, date, currency, wallet_id) "
                "VALUES (?, 'transfer', ?, ?, ?, ?, ?)",
                (user_id, -amount, f"Transfer to {to_name}", get_current_datetime(), from_currency, from_wallet)
            )
            
            # Target wallet transaction
            cursor.execute(
                "INSERT INTO transactions (user_id, type, amount, description, date, currency, wallet_id) "
                "VALUES (?, 'transfer', ?, ?, ?, ?, ?)",
                (user_id, converted_amount, f"Transfer from {from_name}", get_current_datetime(), to_currency, to_wallet)
            )
            
            conn.commit()
            
            # Get updated balances
            cursor.execute("SELECT balance FROM wallets WHERE id = ?", (from_wallet,))
            new_from_balance = cursor.fetchone()[0]
            cursor.execute("SELECT balance FROM wallets WHERE id = ?", (to_wallet,))
            new_to_balance = cursor.fetchone()[0]
            
            await update.message.reply_text(
                f"✅ Transfer successful!\n\n"
                f"From {from_name}:\n"
                f"- Sent: {format_money(amount, from_currency)}\n"
                f"- New balance: {format_money(new_from_balance, from_currency)}\n\n"
                f"To {to_name}:\n"
                f"- Received: {format_money(converted_amount, to_currency)}\n"
                f"- New balance: {format_money(new_to_balance, to_currency)}",
                reply_markup=ReplyKeyboardMarkup(main_keyboard, resize_keyboard=True))
            
            # Clear transfer data
            context.user_data.pop('transfer', None)
            
            return MAIN_MENU
            
    except ValueError as e:
        await update.message.reply_text(
            f"❌ Error: {str(e)}\n\n"
            "Please enter a valid amount to transfer:",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Cancel", callback_data="cancel_transfer")]]))
        return WALLET_MENU
    except Exception as e:
        logger.error(f"Transfer error: {str(e)}")
        await update.message.reply_text(
            "❌ An error occurred during transfer. Please try again.",
            reply_markup=ReplyKeyboardMarkup(main_keyboard, resize_keyboard=True))
        return MAIN_MENU

async def cancel_transfer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancel transfer operation"""
    query = update.callback_query
    await query.answer()
    
    context.user_data.pop('transfer', None)
    await query.edit_message_text(
        "Transfer cancelled",
        reply_markup=ReplyKeyboardMarkup(main_keyboard, resize_keyboard=True))
    return MAIN_MENU

async def manage_hold_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.message.from_user.id
    
    with get_db_connection() as conn:
        holds = conn.execute("SELECT * FROM holds WHERE user_id = ?", (user_id,)).fetchall()

    if not holds:
        await update.message.reply_text(
            "No holds available!",
            reply_markup=ReplyKeyboardMarkup(main_keyboard, resize_keyboard=True))
        return MAIN_MENU

    keyboard = [
        [InlineKeyboardButton(
            f"{format_money(hold['amount'], hold['currency'])} - {hold['description']}", 
            callback_data=f"hold_{hold['id']}"
        )] 
        for hold in holds
    ]
    keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="back_holds")])

    await update.message.reply_text(
        "Select a hold to manage:",
        reply_markup=InlineKeyboardMarkup(keyboard))
    return MANAGE_HOLDS

async def hold_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    hold_id = int(query.data.split('_')[1])
    context.user_data['current_hold'] = hold_id

    with get_db_connection() as conn:
        hold = conn.execute("SELECT * FROM holds WHERE id = ?", (hold_id,)).fetchone()

    currency = hold['currency']
    
    await query.edit_message_text(
        text=f"Hold selected: {format_money(hold['amount'], currency)} - {hold['description']}\nChoose action:",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("➡️ To Income", callback_data=f"transfer_income_{hold_id}"),
             InlineKeyboardButton("⬅️ To Expense", callback_data=f"transfer_outcome_{hold_id}")],
            [InlineKeyboardButton("❌ Remove", callback_data=f"remove_{hold_id}")],
            [InlineKeyboardButton("🔙 Back", callback_data="back_holds")]
        ]))
    return MANAGE_HOLDS

async def transfer_hold(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    action, hold_id = query.data.split('_')[1], int(query.data.split('_')[2])
    user_id = query.from_user.id

    with get_db_connection() as conn:
        hold = conn.execute("SELECT * FROM holds WHERE id = ?", (hold_id,)).fetchone()
        currency = hold['currency']
        
        # Create transaction
        trans_type = 'income' if action == 'income' else 'outcome'
        amount = hold['amount'] if action == 'income' else -hold['amount']
        description = f"{'Released' if action == 'income' else 'Spent'} hold: {hold['description']}"
        
        conn.execute(
            "INSERT INTO transactions (user_id, type, amount, description, date, currency) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, trans_type, amount, description, get_current_datetime(), currency)
        )
        
        # Remove hold
        conn.execute("DELETE FROM holds WHERE id = ?", (hold_id,))
        conn.commit()

    await query.edit_message_text("✅ Hold processed successfully!")
    return await show_balance_menu(update, context)

async def remove_hold(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    hold_id = int(query.data.split('_')[1])

    with get_db_connection() as conn:
        conn.execute("DELETE FROM holds WHERE id = ?", (hold_id,))
        conn.commit()

    await query.edit_message_text("✅ Hold removed successfully!")
    return await show_balance_menu(update, context)

async def start_over(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    reply_text = "Back to main menu:"
    reply_markup = ReplyKeyboardMarkup(main_keyboard, resize_keyboard=True)
    
    if update.callback_query:
        await update.callback_query.message.reply_text(reply_text, reply_markup=reply_markup)
    else:
        await update.message.reply_text(reply_text, reply_markup=reply_markup)
    return MAIN_MENU

async def settings_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "⚙️ Settings Menu",
        reply_markup=ReplyKeyboardMarkup(settings_keyboard, resize_keyboard=True))
    return SETTINGS_MENU

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "Action cancelled",
        reply_markup=ReplyKeyboardMarkup(main_keyboard, resize_keyboard=True))
    return MAIN_MENU

async def show_recent_transactions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    currency = get_user_currency(user_id)
    
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM transactions WHERE user_id = ? "
            "ORDER BY date DESC LIMIT 5",
            (user_id,)
        )
        recent_transactions = cursor.fetchall()

    if not recent_transactions:
        await query.edit_message_caption(
            caption="No recent transactions found!",
            reply_markup=None
        )
        return MAIN_MENU

    trans_text = "📜 *Recent Transactions*\n\n"
    for t in recent_transactions:
        date = format_transaction_date(t['date'])
        trans_type = "🟢 Income" if t['type'] == 'income' else "🔴 Expense"
        amount = t['amount'] if t['amount'] > 0 else -t['amount']
        trans_currency = t['currency'] if 'currency' in t.keys() else currency
        description = t['description'][:20] + '...' if len(t['description']) > 20 else t['description']
        
        trans_text += (
            f"**{trans_type}**\n"
            f"💸 Amount: {format_money(amount, trans_currency)}\n"
            f"📅 Date: {date}\n"
            f"📝 {description}\n\n"
        )
    
    # Add "Back to Summary" button
    keyboard = [[InlineKeyboardButton("🔙 Back to Summary", callback_data="back_to_summary")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    # Edit the caption of the existing photo message
    await query.edit_message_caption(
        caption=trans_text,
        parse_mode='Markdown',
        reply_markup=reply_markup
    )
    
    return MAIN_MENU

async def back_to_summary(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    
    # Delete the previous message
    try:
        await query.message.delete()
    except Exception as e:
        logger.error(f"Error deleting message: {e}")
    
    # Show the balance summary
    return await show_balance(update, context)

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log errors caused by updates."""
    logger.error('Update "%s" caused error "%s"', update, context.error)
    if update.effective_message:
        await update.effective_message.reply_text(
            "An error occurred. Please try again.",
            reply_markup=ReplyKeyboardMarkup(main_keyboard, resize_keyboard=True)
        )

def main() -> None:
    # Initialize database
    init_db()

    # Create application with job queue
    try:
        TOKEN = os.getenv("TELEGRAM_TOKEN", "8017763140:AAHD8fI9orJbnIyljuWF8PZN6yBmDxKuPrw")
        application = Application.builder().token(TOKEN).build()
    except Exception as e:
        logger.error(f"Failed to create application: {e}")
        return

    application.add_error_handler(error_handler)

    job_queue = application.job_queue
    if job_queue:
        try:
            job_queue.run_repeating(
                process_recurring_transactions,
                interval=datetime.timedelta(days=1),
                first=datetime.time(hour=0, minute=0, tzinfo=TIMEZONE)
            )
            job_queue.run_repeating(
                notify_budget_updates,
                interval=datetime.timedelta(days=7),
                first=datetime.time(hour=9, minute=0, tzinfo=TIMEZONE)
            )
        except Exception as e:
            logger.error(f"Failed to schedule jobs: {str(e)}")

    # Conversation handler
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler('start', start)],
        states={
            MAIN_MENU: [
            MessageHandler(filters.Regex(r'^💰 Balance$'), show_balance_menu),
            MessageHandler(filters.Regex(r'^📥 Income$'), income_menu),
            MessageHandler(filters.Regex(r'^📤 Outcome$'), outcome_menu),
            MessageHandler(filters.Regex(r'^⏳ Hold$'), holds_menu),
            MessageHandler(filters.Regex(r'^⚙️ Settings$'), settings_menu),
            CallbackQueryHandler(show_recent_transactions, pattern=r"^show_recent_trans$"),
            CallbackQueryHandler(back_to_summary, pattern=r"^back_to_summary$"),
        ],
        SETTINGS_MENU: [
            MessageHandler(filters.Regex(r'^💱 Currency$'), currency_menu),
            MessageHandler(filters.Regex(r'^👛 Wallets$'), wallets_menu),
            MessageHandler(filters.Regex(r'^🔄 Recurring$'), recurring_menu),
            MessageHandler(filters.Regex(r'^📊 Budget$'), set_budget),
            MessageHandler(filters.Regex(r'^📈 Report$'), generate_report),
            MessageHandler(filters.Regex(r'^📦 Backup$'), backup_data),
            MessageHandler(filters.Regex(r'^🗑 Transactions$'), show_transactions_for_deletion),
            MessageHandler(filters.Regex(r'^🔙 Back$'), start_over),
            ],
            WALLET_MENU: [
                MessageHandler(filters.Regex(r'^➕ Add Wallet$'), add_wallet_prompt),
                MessageHandler(filters.Regex(r'^🏷 Set Default Wallet$'), set_default_wallet),
                MessageHandler(filters.Regex(r'^💸 Transfer Funds$'), transfer_funds_prompt),
                MessageHandler(filters.Regex(r'^🔙 Back$'), start_over),
                CallbackQueryHandler(handle_set_default, pattern=r"^setdef_"),
                CallbackQueryHandler(select_target_wallet, pattern=r"^transfer_from_"),
                CallbackQueryHandler(enter_transfer_amount, pattern=r"^transfer_to_"),
                CallbackQueryHandler(cancel_transfer, pattern=r"^cancel_transfer$"),
                CallbackQueryHandler(transfer_funds_prompt, pattern=r"^back_transfer$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, process_transfer)
            ],
            ADD_WALLET: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_wallet),
                MessageHandler(filters.Regex(r'^❌ Cancel$'), cancel)
            ],
            ADD_INCOME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_income),
                MessageHandler(filters.Regex(r'^❌ Cancel$'), cancel)
            ],
            ADD_OUTCOME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_outcome),
                MessageHandler(filters.Regex(r'^❌ Cancel$'), cancel)
            ],
            ADD_HOLD: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_hold),
                MessageHandler(filters.Regex(r'^❌ Cancel$'), cancel)
            ],
            RECURRING_MENU: [
                MessageHandler(filters.Regex(r'^➕ Add Recurring$'), add_recurring_prompt),
                MessageHandler(filters.Regex(r'^📋 List Recurring$'), list_recurring),
                MessageHandler(filters.Regex(r'^🔙 Back$'), start_over),
                CallbackQueryHandler(manage_recurring, pattern=r"^recur_"),
                CallbackQueryHandler(remove_recurring, pattern=r"^remove_recur_"),
                CallbackQueryHandler(edit_recurring_prompt, pattern=r"^edit_recur_"),
                CallbackQueryHandler(start_over, pattern=r"^back_recur_list"),
                CallbackQueryHandler(start_over, pattern=r"^back_recurring"),
            ],
            ADD_RECURRING: [
                MessageHandler(filters.Regex(r'^(Income|Outcome)$'), add_recurring_type),
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_recurring),
                MessageHandler(filters.TEXT & ~filters.COMMAND, edit_recurring),
                MessageHandler(filters.Regex(r'^❌ Cancel$'), cancel)
            ],
            CURRENCY_MENU: [
                CallbackQueryHandler(set_currency, pattern=r"^currency_"),
                CallbackQueryHandler(start_over, pattern=r"^back_currency")
            ],
            MANAGE_HOLDS: [
                MessageHandler(filters.Regex(r'^➕ Add Hold$'), add_hold_prompt),
    MessageHandler(filters.Regex(r'^🛠 Manage Hold$'), manage_hold_menu),
    MessageHandler(filters.Regex(r'^🔙 Back$'), start_over),
    CallbackQueryHandler(hold_action, pattern=r"^hold_"),
    CallbackQueryHandler(transfer_hold, pattern=r"^transfer_(income|outcome)_"),
    CallbackQueryHandler(remove_hold, pattern=r"^remove_"),
    CallbackQueryHandler(start_over, pattern=r"^back_holds"),
                CallbackQueryHandler(hold_action, pattern=r"^hold_"),
                CallbackQueryHandler(transfer_hold, pattern=r"^transfer_(income|outcome)_"),
                CallbackQueryHandler(remove_hold, pattern=r"^remove_"),
                CallbackQueryHandler(start_over, pattern=r"^back_holds")
            ],
            MANAGE_TRANSACTIONS: [
                CallbackQueryHandler(delete_transaction, pattern=r"^del_trans_"),
                CallbackQueryHandler(start_over, pattern=r"^back_transactions")
            ],
            SET_BUDGET: [
                CallbackQueryHandler(budget_category_selected, pattern=r"^budgetcat_"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, save_budget),
                CallbackQueryHandler(start_over, pattern=r"^cancel_budget$"),
            ],
            REPORT_MENU: [
                CallbackQueryHandler(show_report, pattern=r"^report_"),
                MessageHandler(filters.Regex(r'^🔙 Back$'), start_over)
            ]
        },
        fallbacks=[CommandHandler('start', start)],
        allow_reentry=True
    )

    application.add_handler(conv_handler)
    application.add_handler(CommandHandler("updatecode1", update_code1))

    # Start the Bot
    try:
        application.run_polling()
    except Exception as e:
        logger.error(f"Bot failed to start: {e}")

if __name__ == '__main__':
    main()
