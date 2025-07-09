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
            
        # Create/modify holds table with wallet_id
        cursor.execute('''CREATE TABLE IF NOT EXISTS holds
                     (id INTEGER PRIMARY KEY, 
                      user_id INTEGER, 
                      amount REAL, 
                      description TEXT, 
                      currency TEXT,
                      wallet_id INTEGER,
                      tags TEXT DEFAULT '',
                      FOREIGN KEY(wallet_id) REFERENCES wallets(id))''')
        
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
SET_BUDGET, REPORT_MENU, BACKUP, WALLET_MENU, ADD_WALLET, SETTINGS_MENU, CHOOSE_WALLET_INCOME, CHOOSE_WALLET_OUTCOME, ADD_HOLD_FROM_WALLET, \
CHOOSE_WALLET_FOR_HOLD, EDIT_HOLD, TRANSFER_AMOUNT, TRANSFER_TARGET = range(22)

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
    'Housing': ['rent', 'mortgage', 'utilities', 'electricity', 'water', 'internet', 'room'],
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

async def wallet_actions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    wallet_id = int(query.data.split('_')[1])
    context.user_data['current_wallet'] = wallet_id
    
    with get_db_connection() as conn:
        wallet = conn.execute("SELECT * FROM wallets WHERE id = ?", (wallet_id,)).fetchone()
    
    currency = wallet['currency']
    balance = format_money(wallet['balance'], currency)
    
    await query.edit_message_text(
        text=f"Wallet: {wallet['name']} ({currency})\n"
             f"Balance: {balance}\n\n"
             "Choose action:",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("💸 Transfer Funds", callback_data=f"transfer_from_{wallet_id}")],
            [InlineKeyboardButton("⏳ Add Hold", callback_data=f"add_hold_{wallet_id}")],
            [InlineKeyboardButton("🔙 Back to Wallets", callback_data="back_wallets")]
        ]))
    return WALLET_MENU

async def add_hold_from_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    wallet_id = int(query.data.split('_')[2])
    context.user_data['hold_wallet'] = wallet_id
    
    with get_db_connection() as conn:
        wallet = conn.execute("SELECT * FROM wallets WHERE id = ?", (wallet_id,)).fetchone()
    
    await query.edit_message_text(
        f"⏳ Adding hold from {wallet['name']}\n"
        f"Available: {format_money(wallet['balance'], wallet['currency'])}\n\n"
        "Enter amount to hold:",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="back_wallet_actions")]])
    )
    return ADD_HOLD_FROM_WALLET

async def process_hold_from_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.message.from_user.id
    amount_text = update.message.text.replace(',', '.')  # Handle decimal separators
    
    try:
        amount = float(amount_text)
        if amount <= 0:
            raise ValueError("Amount must be positive")
            
        wallet_id = context.user_data.get('hold_wallet')
        if not wallet_id:
            await update.message.reply_text(
                "Wallet not selected. Please start over.",
                reply_markup=ReplyKeyboardMarkup(main_keyboard, resize_keyboard=True))
            return MAIN_MENU
        
        with get_db_connection() as conn:
            # Get wallet details
            cursor = conn.cursor()
            cursor.execute(
                "SELECT balance, currency FROM wallets WHERE id = ? AND user_id = ?",
                (wallet_id, user_id)
            )
            wallet = cursor.fetchone()
            
            if not wallet:
                raise ValueError("Wallet not found")
                
            if amount > wallet['balance']:
                raise ValueError(f"Insufficient funds. Available: {format_money(wallet['balance'], wallet['currency'])}")
            
            # Create hold with wallet_id
            description = f"Hold from {get_wallet_name(wallet_id)}"
            cursor.execute(
                "INSERT INTO holds (user_id, amount, description, currency, wallet_id) "
                "VALUES (?, ?, ?, ?, ?)",
                (user_id, amount, description, wallet['currency'], wallet_id)
            )
            
            # Deduct from wallet
            cursor.execute(
                "UPDATE wallets SET balance = balance - ? WHERE id = ?",
                (amount, wallet_id)
            )
            
            conn.commit()
            
            await update.message.reply_text(
                f"✅ Hold created: {format_money(amount, wallet['currency'])}\n"
                f"From wallet: {get_wallet_name(wallet_id)}\n"
                f"New balance: {format_money(wallet['balance'] - amount, wallet['currency'])}",
                reply_markup=ReplyKeyboardMarkup(main_keyboard, resize_keyboard=True))
            
            # Clear context
            context.user_data.pop('hold_wallet', None)
            return MAIN_MENU
            
    except ValueError as e:
        await update.message.reply_text(
            f"❌ Error: {str(e)}\n\n"
            "Please enter a valid amount:",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Cancel", callback_data="cancel_hold")]]))
        return ADD_HOLD_FROM_WALLET
    except Exception as e:
        logger.error(f"Hold creation failed: {str(e)}")
        await update.message.reply_text(
            "❌ An error occurred. Please try again.",
            reply_markup=ReplyKeyboardMarkup(main_keyboard, resize_keyboard=True))
        return MAIN_MENU

async def wallets_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        # Get user from either message or callback query
        if update.message:
            user = update.message.from_user
            reply_method = update.message.reply_text
        elif update.callback_query:
            user = update.callback_query.from_user
            reply_method = update.callback_query.message.reply_text
        else:
            logger.error("Invalid update received in wallets_menu")
            return MAIN_MENU

        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM wallets WHERE user_id = ?", (user.id,))
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
        
        await reply_method(
            text,
            reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True))
        
        return WALLET_MENU

    except Exception as e:
        logger.error(f"Error in wallets_menu: {str(e)}")
        if update.effective_message:
            await update.effective_message.reply_text(
                "An error occurred. Returning to main menu.",
                reply_markup=ReplyKeyboardMarkup(main_keyboard, resize_keyboard=True))
        return MAIN_MENU

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
    #keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="back")])
    
    await update.message.reply_text(
        "📈 Select report type:",
        reply_markup=InlineKeyboardMarkup(keyboard))
    return REPORT_MENU

async def show_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Display requested report"""
    query = update.callback_query
    await query.answer()
    report_type = query.data.split('_')[1]
    
    if report_type == "back_to_reports":
        return await settings_menu(update, context)
        
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
        await query.edit_message_text(
            text,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Back", callback_data="back_to_reports")]
            ])
        )
        return REPORT_MENU
        
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
        caption="Here's your financial data backup 📦",
        reply_markup=ReplyKeyboardMarkup(settings_keyboard, resize_keyboard=True)
    )
    return SETTINGS_MENU

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
    user_id = query.from_user.id

    with get_db_connection() as conn:
        try:
            # Get transaction details before deleting
            cursor = conn.cursor()
            trans = cursor.execute(
                "SELECT type, amount, wallet_id, currency FROM transactions WHERE id = ? AND user_id = ?",
                (trans_id, user_id)
            ).fetchone()

            if not trans:
                await query.edit_message_text("❌ Transaction not found!")
                return await show_transactions_for_deletion(update, context)

            # Delete the transaction
            cursor.execute("DELETE FROM transactions WHERE id = ?", (trans_id,))
            
            # Update wallet balance
            if trans['wallet_id']:
                adjustment = -trans['amount'] if trans['type'] == 'income' else trans['amount']
                cursor.execute(
                    "UPDATE wallets SET balance = balance + ? WHERE id = ?",
                    (adjustment, trans['wallet_id'])
                )
            
            conn.commit()

            # Show confirmation and refresh
            await query.edit_message_text(
                "✅ Transaction deleted successfully!",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔙 Back to Transactions", callback_data="back_to_transactions")]
                ])
            )
            return MAIN_MENU

        except Exception as e:
            logger.error(f"Error deleting transaction: {str(e)}")
            await query.edit_message_text(
                "❌ Failed to delete transaction",
                reply_markup=ReplyKeyboardMarkup(main_keyboard, resize_keyboard=True)
            )
            return MAIN_MENU

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
        
        # Get fresh financial data
        cursor.execute("SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE user_id = ? AND type = 'income'", (user_id,))
        income = cursor.fetchone()[0]
        
        cursor.execute("SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE user_id = ? AND type = 'outcome'", (user_id,))
        outcome = cursor.fetchone()[0]
        
        cursor.execute("SELECT COALESCE(SUM(amount), 0) FROM holds WHERE user_id = ?", (user_id,))
        holds = cursor.fetchone()[0]
        
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
        
        f"<b>💳 𝗧𝗼𝘁𝗮𝗹 𝗕𝗮𝗹𝗮𝗻𝗰𝗲:</b> {fm(total_balance):>16}\n\n"
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
    try:
        query = update.callback_query
        await query.answer()
        
        wallet_id = int(query.data.split('_')[1])
        user_id = query.from_user.id
        
        with get_db_connection() as conn:
            cursor = conn.cursor()
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
            
            # Create a new inline keyboard with just a "Back" button
            keyboard = InlineKeyboardMarkup([
                #[InlineKeyboardButton("🔙 Back to Wallets", callback_data="back_wallets")]
            ])
            
            await query.edit_message_text(
                f"✅ Default wallet set to: {wallet['name']} ({wallet['currency']})",
                reply_markup=keyboard)
            
            return WALLET_MENU
            
    except (IndexError, ValueError) as e:
        logger.error(f"Invalid callback data: {str(e)}")
        if update.callback_query:
            await update.callback_query.edit_message_text("❌ Invalid wallet selection")
        return await wallets_menu(update, context)
    except sqlite3.Error as e:
        logger.error(f"Database error setting default wallet: {str(e)}")
        if update.callback_query:
            await update.callback_query.edit_message_text("❌ Error updating wallet")
        return await wallets_menu(update, context)
    except Exception as e:
        logger.error(f"Unexpected error in handle_set_default: {str(e)}")
        if update.callback_query:
            await update.callback_query.edit_message_text("❌ An error occurred")
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
    keyboard.append([InlineKeyboardButton("🔙 Back to Settings", callback_data="back_currency")])
    
    if update.message:
        await update.message.reply_text(
            "Select your default currency:",
            reply_markup=InlineKeyboardMarkup(keyboard))
    elif update.callback_query:
        await update.callback_query.message.reply_text(
            "Select your default currency:",
            reply_markup=InlineKeyboardMarkup(keyboard))
    
    return CURRENCY_MENU

async def set_currency(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    currency_code = query.data.split('_')[1]
    user_id = query.from_user.id
    
    with get_db_connection() as conn:
        conn.execute(
            "UPDATE user_settings SET default_currency = ? WHERE user_id = ?",
            (currency_code, user_id)
        )
        conn.commit()
    
    # Edit the message instead of sending a new one
    await query.edit_message_text(
        f"✅ Default currency set to {currency_code} {CURRENCIES.get(currency_code, '')}",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 Back to Settings", callback_data="back_to_settings")]
        ])
    )
    return CURRENCY_MENU

async def back_to_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    return await settings_menu(update, context)

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
    
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id, name, currency FROM wallets WHERE user_id = ?", (user_id,))
        wallets = cursor.fetchall()
    
    if not wallets:
        await update.message.reply_text(
            "No wallets available. Add a wallet first!",
            reply_markup=ReplyKeyboardMarkup(main_keyboard, resize_keyboard=True))
        return MAIN_MENU
    
    keyboard = [
        [InlineKeyboardButton(
            f"{wallet['name']} ({wallet['currency']})", 
            callback_data=f"income_wallet_{wallet['id']}"
        )]
        for wallet in wallets
    ]
    keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="back_income")])
    
    await update.message.reply_text(
        "📥 Select wallet for income:",
        reply_markup=InlineKeyboardMarkup(keyboard))
    return CHOOSE_WALLET_INCOME

async def add_income(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.message.from_user.id
    text = update.message.text.split()
    
    # Get selected wallet from context
    wallet_id = context.user_data.get('income_wallet')
    if not wallet_id:
        await update.message.reply_text(
            "Wallet not selected. Please start over.",
            reply_markup=ReplyKeyboardMarkup(main_keyboard, resize_keyboard=True))
        return MAIN_MENU
    
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT currency FROM wallets WHERE id = ?", (wallet_id,))
        wallet_currency = cursor.fetchone()[0]
    
    try:
        # Parse input
        if len(text) > 1 and text[1].upper() in CURRENCIES:
            amount = float(text[0])
            currency = text[1].upper()
            description = ' '.join(text[2:]) if len(text) > 2 else "Income"
        else:
            amount = float(text[0])
            currency = wallet_currency
            description = ' '.join(text[1:]) if len(text) > 1 else "Income"
        
        # Convert amount to wallet currency if needed
        if currency != wallet_currency:
            converted_amount = convert_currency(amount, currency, wallet_currency)
            currency = wallet_currency
            amount = converted_amount
        
        # Insert transaction
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO transactions (user_id, type, amount, description, date, currency, wallet_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (user_id, 'income', amount, description, get_current_datetime(), currency, wallet_id)
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
        
        # Clear temporary data
        context.user_data.pop('income_wallet', None)
        return MAIN_MENU

    except (ValueError, IndexError):
        await update.message.reply_text(
            "❌ Invalid format. Please enter: Amount [Currency] [Description]",
            reply_markup=ReplyKeyboardMarkup(cancel_keyboard, resize_keyboard=True))
        return ADD_INCOME

async def back_to_transactions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    return await show_transactions_for_deletion(update, context)

async def outcome_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.message.from_user.id
    
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id, name, currency FROM wallets WHERE user_id = ?", (user_id,))
        wallets = cursor.fetchall()
    
    if not wallets:
        await update.message.reply_text(
            "No wallets available. Add a wallet first!",
            reply_markup=ReplyKeyboardMarkup(main_keyboard, resize_keyboard=True))
        return MAIN_MENU
    
    keyboard = [
        [InlineKeyboardButton(
            f"{wallet['name']} ({wallet['currency']})", 
            callback_data=f"outcome_wallet_{wallet['id']}"
        )]
        for wallet in wallets
    ]
    keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="back_outcome")])
    
    await update.message.reply_text(
        "📤 Select wallet for expense:",
        reply_markup=InlineKeyboardMarkup(keyboard))
    return CHOOSE_WALLET_OUTCOME

async def wallet_chosen_income(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    wallet_id = int(query.data.split('_')[2])
    context.user_data['income_wallet'] = wallet_id
    
    currency = ""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT currency FROM wallets WHERE id = ?", (wallet_id,))
        wallet = cursor.fetchone()
        if wallet:
            currency = wallet['currency']
    
    await query.edit_message_text(
        f"➕ Add income to {get_wallet_name(wallet_id)} (currency: {currency}):\n"
        "<b>Amount</b> (e.g., 1000)\n"
        "<b>Amount Currency</b> (e.g., 1000 EUR)\n"
        "<b>Amount Currency Description</b> (e.g., 1000 EUR Salary)",
        parse_mode="HTML")
    return ADD_INCOME

async def wallet_chosen_outcome(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    wallet_id = int(query.data.split('_')[2])
    context.user_data['outcome_wallet'] = wallet_id
    
    currency = ""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT currency FROM wallets WHERE id = ?", (wallet_id,))
        wallet = cursor.fetchone()
        if wallet:
            currency = wallet['currency']
    
    await query.edit_message_text(
        f"➖ Add expense from {get_wallet_name(wallet_id)} (currency: {currency}):\n"
        "<b>Amount</b> (e.g., 50)\n"
        "<b>Amount Description</b> (e.g., 50 Groceries)\n"
        "<b>Amount Currency Description</b> (e.g., 50 EUR Dinner)",
        parse_mode="HTML")
    return ADD_OUTCOME

async def add_outcome(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.message.from_user.id
    text = update.message.text.split()
    
    # Get selected wallet from context
    wallet_id = context.user_data.get('outcome_wallet')
    if not wallet_id:
        await update.message.reply_text(
            "Wallet not selected. Please start over.",
            reply_markup=ReplyKeyboardMarkup(main_keyboard, resize_keyboard=True))
        return MAIN_MENU
    
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT currency FROM wallets WHERE id = ?", (wallet_id,))
        wallet_currency = cursor.fetchone()[0]
    
    try:
        # Parse currency if provided
        if len(text) > 1 and text[1].upper() in CURRENCIES:
            amount = float(text[0])
            currency = text[1].upper()
            description = ' '.join(text[2:]) if len(text) > 2 else "Expense"
        else:
            amount = float(text[0])
            currency = wallet_currency
            description = ' '.join(text[1:]) if len(text) > 1 else "Expense"
        
        # Auto-detect category based on description
        category = detect_category(description)
        
        # Convert amount to wallet currency if needed
        if currency != wallet_currency:
            converted_amount = convert_currency(amount, currency, wallet_currency)
            amount = converted_amount
            currency = wallet_currency
        
        with get_db_connection() as conn:
            cursor = conn.cursor()
            
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
        
        # Clear temporary data
        context.user_data.pop('outcome_wallet', None)
        return MAIN_MENU

    except (ValueError, IndexError):
        await update.message.reply_text(
            "❌ Invalid format. Please enter: Amount [Currency] [Description]",
            reply_markup=ReplyKeyboardMarkup(cancel_keyboard, resize_keyboard=True))
        return ADD_OUTCOME

async def holds_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        if not update.effective_message or not update.effective_user:
            logger.error("Invalid update received in holds_menu")
            return MAIN_MENU

        user_id = update.effective_user.id
        
        with get_db_connection() as conn:
            holds = conn.execute("SELECT * FROM holds WHERE user_id = ?", (user_id,)).fetchall()

        if not holds:
            text = "No holds found! How would you like to add a hold?"
            keyboard = [
                ['➕ Normal Hold', '➕ From Wallet'],
                ['🔙 Back']
            ]
        else:
            # Fix: Use dictionary access with fallback instead of .get()
            holds_list = "\n".join(
                f"{idx+1}. {(hold['tags'] + ' ') if 'tags' in hold and hold['tags'] else ''}{hold['description']}: {format_money(hold['amount'], hold['currency'])}" 
                for idx, hold in enumerate(holds)
            )
            text = f"⏳ Your holds:\n{holds_list}\n\nSelect how to add:"
            keyboard = [
                ['➕ Normal Hold', '➕ From Wallet'],
                ['🛠 Manage Hold'],
                ['🔙 Back']
            ]
        
        await update.effective_message.reply_text(
            text,
            reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True))
        
        return MANAGE_HOLDS

    except Exception as e:
        logger.error(f"Error in holds_menu: {str(e)}")
        if update.effective_message:
            await update.effective_message.reply_text(
                "An error occurred. Returning to main menu.",
                reply_markup=ReplyKeyboardMarkup(main_keyboard, resize_keyboard=True))
        return MAIN_MENU

async def choose_wallet_for_hold(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.message.from_user.id
    
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id, name, currency, balance FROM wallets WHERE user_id = ?", (user_id,))
        wallets = cursor.fetchall()
    
    if not wallets:
        await update.message.reply_text(
            "No wallets available. Add a wallet first!",
            reply_markup=ReplyKeyboardMarkup(main_keyboard, resize_keyboard=True))
        return MAIN_MENU
    
    keyboard = [
        [InlineKeyboardButton(
            f"{wallet['name']} ({format_money(wallet['balance'], wallet['currency'])})", 
            callback_data=f"hold_wallet_{wallet['id']}"
        )]
        for wallet in wallets
    ]
    keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="back_holds")])
    
    await update.message.reply_text(
        "👛 Select wallet to hold from:",
        reply_markup=InlineKeyboardMarkup(keyboard))
    return CHOOSE_WALLET_FOR_HOLD

async def wallet_chosen_for_hold(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    wallet_id = int(query.data.split('_')[2])
    context.user_data['hold_wallet'] = wallet_id
    
    with get_db_connection() as conn:
        wallet = conn.execute("SELECT * FROM wallets WHERE id = ?", (wallet_id,)).fetchone()
    
    await query.edit_message_text(
        f"⏳ Adding hold from {wallet['name']}\n"
        f"Available: {format_money(wallet['balance'], wallet['currency'])}\n\n"
        "Enter amount to hold:",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="back_choose_wallet_hold")]])
    )
    return ADD_HOLD_FROM_WALLET


async def add_hold_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.message.from_user.id
    currency = get_user_currency(user_id)
    await update.message.reply_text(
        f"⏳ Add holds (one per line) in format (currency: {currency}):\n"
        "<b>Amount Description</b>\n"
        "Example:\n"
        "1000 Amazon purchase\n"
        "2000 Groceries\n"
        "500 Restaurant bill\n\n"
        "You can add multiple holds at once by putting each on a new line",
        reply_markup=ReplyKeyboardMarkup(cancel_keyboard, resize_keyboard=True),
        parse_mode="HTML")
    return ADD_HOLD

async def add_hold(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.message.from_user.id
    text = update.message.text
    currency = get_user_currency(user_id)
    success_count = 0
    error_messages = []

    # Split input by lines
    lines = [line.strip() for line in text.split('\n') if line.strip()]
    
    with get_db_connection() as conn:
        cursor = conn.cursor()
        
        for line in lines:
            try:
                # Split each line into amount and description
                parts = line.split(maxsplit=1)
                if len(parts) < 1:
                    error_messages.append(f"❌ Invalid format in line: '{line}'")
                    continue
                
                amount = float(parts[0])
                description = parts[1] if len(parts) > 1 else "Hold"
                
                cursor.execute(
                    "INSERT INTO holds (user_id, amount, description, currency) VALUES (?, ?, ?, ?)",
                    (user_id, amount, description, currency)
                )
                success_count += 1
                
            except ValueError:
                error_messages.append(f"❌ Invalid amount in line: '{line}'")
            except Exception as e:
                error_messages.append(f"❌ Error processing line: '{line}'")
                logger.error(f"Error adding hold: {str(e)}")
        
        conn.commit()

    # Prepare response message
    response = []
    if success_count > 0:
        response.append(f"✅ Added {success_count} hold(s) successfully!")
    if error_messages:
        response.append("\n".join(error_messages))
    
    if not response:  # If somehow nothing was processed
        response.append("No valid holds were processed")

    await update.message.reply_text(
        "\n".join(response),
        reply_markup=ReplyKeyboardMarkup(main_keyboard, resize_keyboard=True))
    return MAIN_MENU

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
    
    try:
        source_wallet_id = int(query.data.split('_')[2])
        user_id = query.from_user.id
        
        with get_db_connection() as conn:
            # Get source wallet details
            source_wallet = conn.execute(
                "SELECT name, currency, balance FROM wallets WHERE id = ?", 
                (source_wallet_id,)
            ).fetchone()
            
            if not source_wallet:
                await query.edit_message_text("Source wallet not found!")
                return WALLET_MENU
                
            # Initialize transfer data with all required fields
            context.user_data['transfer'] = {
                'from': source_wallet_id,
                'from_currency': source_wallet['currency'],
                'from_balance': source_wallet['balance'],
                'from_name': source_wallet['name']
            }
            
            # Get available target wallets (excluding source)
            target_wallets = conn.execute(
                "SELECT id, name, currency FROM wallets WHERE user_id = ? AND id != ?",
                (user_id, source_wallet_id)
            ).fetchall()

        if not target_wallets:
            await query.edit_message_text("No other wallets available for transfer!")
            return WALLET_MENU

        keyboard = [
            [InlineKeyboardButton(
                f"{wallet['name']} ({wallet['currency']})", 
                callback_data=f"transfer_to_{wallet['id']}"
            )]
            for wallet in target_wallets
        ]
        keyboard.append([InlineKeyboardButton("🔙 Cancel", callback_data="cancel_transfer")])

        await query.edit_message_text(
            f"Transferring from: {source_wallet['name']} ({source_wallet['currency']})\n"
            "Select target wallet:",
            reply_markup=InlineKeyboardMarkup(keyboard))
            
        return TRANSFER_TARGET

    except Exception as e:
        logger.error(f"Error in select_target_wallet: {str(e)}")
        await query.edit_message_text(
            "An error occurred. Please try again.",
            reply_markup=ReplyKeyboardMarkup(main_keyboard, resize_keyboard=True))
        return MAIN_MENU

async def enter_transfer_amount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle target wallet selection and prompt for amount"""
    query = update.callback_query
    await query.answer()
    
    try:
        target_wallet_id = int(query.data.split('_')[2])
        user_id = query.from_user.id
        
        with get_db_connection() as conn:
            # Get target wallet details
            target_wallet = conn.execute(
                "SELECT name, currency FROM wallets WHERE id = ?", 
                (target_wallet_id,)
            ).fetchone()
            
            if not target_wallet:
                await query.edit_message_text("Target wallet not found!")
                return WALLET_MENU
                
            # Update transfer data with target info
            context.user_data['transfer'].update({
                'to': target_wallet_id,
                'to_currency': target_wallet['currency'],
                'to_name': target_wallet['name']
            })

        await query.edit_message_text(
            f"💸 Transfer from {context.user_data['transfer']['from_name']} ({context.user_data['transfer']['from_currency']}) "
            f"to {target_wallet['name']} ({target_wallet['currency']})\n\n"
            f"Available: {format_money(context.user_data['transfer']['from_balance'], context.user_data['transfer']['from_currency'])}\n"
            "Enter amount to transfer:",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Cancel", callback_data="cancel_transfer")]])
        )
        return TRANSFER_AMOUNT

    except Exception as e:
        logger.error(f"Error in enter_transfer_amount: {str(e)}")
        await query.edit_message_text(
            "An error occurred. Please try again.",
            reply_markup=ReplyKeyboardMarkup(main_keyboard, resize_keyboard=True))
        return MAIN_MENU

async def process_transfer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Process the wallet-to-wallet transfer"""
    user_id = update.message.from_user.id
    amount_text = update.message.text.replace(',', '.')
    
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
        return TRANSFER_AMOUNT  # Stay in transfer amount state
            
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
    # Use inline keyboard for edited message
    await query.edit_message_text(
        "Transfer cancelled",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 Back to Wallets", callback_data="back_wallets")]
        ])
    )
    return WALLET_MENU

async def manage_hold_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        if not update.effective_message or not update.effective_user:
            logger.error("Invalid update received in manage_hold_menu")
            return MAIN_MENU

        user_id = update.effective_user.id
        
        with get_db_connection() as conn:
            holds = conn.execute("SELECT * FROM holds WHERE user_id = ?", (user_id,)).fetchall()

        if not holds:
            await update.effective_message.reply_text(
                "No holds available!",
                reply_markup=ReplyKeyboardMarkup(main_keyboard, resize_keyboard=True))
            return MAIN_MENU

        # Fix: Use direct dictionary-style access with existence check
        keyboard = [
            [InlineKeyboardButton(
                f"{(hold['tags'] + ' ') if 'tags' in hold and hold['tags'] else ''}{hold['description']}: {format_money(hold['amount'], hold['currency'])}", 
                callback_data=f"hold_{hold['id']}"
            )] 
            for hold in holds
        ]
        keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="back_holds")])

        await update.effective_message.reply_text(
            "Select a hold to manage:",
            reply_markup=InlineKeyboardMarkup(keyboard))
        return MANAGE_HOLDS

    except Exception as e:
        logger.error(f"Error in manage_hold_menu: {str(e)}")
        if update.effective_message:
            await update.effective_message.reply_text(
                "An error occurred. Returning to main menu.",
                reply_markup=ReplyKeyboardMarkup(main_keyboard, resize_keyboard=True))
        return MAIN_MENU

async def hold_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    hold_id = int(query.data.split('_')[1])
    context.user_data['current_hold'] = hold_id

    with get_db_connection() as conn:
        hold = conn.execute("SELECT * FROM holds WHERE id = ?", (hold_id,)).fetchone()
        if not hold:
            await query.edit_message_text("Hold not found!")
            return MAIN_MENU

    # Convert Row to dict for easier access
    hold_dict = dict(hold)
    currency = hold_dict['currency']
    tags = hold_dict.get('tags', '') or ''  # Handle case where tags might be None
    
    await query.edit_message_text(
        text=f"📌 Hold: {format_money(hold_dict['amount'], currency)}\n"
             f"📝 Description: {tags}{hold_dict['description']}\n\n"
             "Choose action:",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✏️ Rename", callback_data=f"rename_{hold_id}"),
             InlineKeyboardButton("🏷 Add Tag", callback_data=f"tag_{hold_id}")],
            [InlineKeyboardButton("➡️ To Income", callback_data=f"transfer_income_{hold_id}"),
             InlineKeyboardButton("⬅️ To Expense", callback_data=f"transfer_outcome_{hold_id}")],
            [InlineKeyboardButton("❌ Remove", callback_data=f"remove_{hold_id}")],
            [InlineKeyboardButton("🔙 Back", callback_data="back_holds")]
        ]))
    return MANAGE_HOLDS

async def rename_hold_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    hold_id = int(query.data.split('_')[1])
    context.user_data['editing_hold'] = hold_id

    with get_db_connection() as conn:
        hold = conn.execute("SELECT * FROM holds WHERE id = ?", (hold_id,)).fetchone()
        if not hold:
            await query.edit_message_text("Hold not found!")
            return MAIN_MENU
        hold_dict = dict(hold)

    await query.edit_message_text(
        f"✏️ Editing hold: {format_money(hold_dict['amount'], hold_dict['currency'])}\n"
        f"Current description: {hold_dict.get('tags', '') or ''}{hold_dict['description']}\n\n"
        "Enter new description:",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Cancel", callback_data=f"hold_{hold_id}")]])
    )
    return EDIT_HOLD

async def add_tag_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    hold_id = int(query.data.split('_')[1])
    context.user_data['tagging_hold'] = hold_id

    keyboard = [
        [InlineKeyboardButton("🟢 REFUND", callback_data=f"addtag_{hold_id}_🟢 REFUND")],
        [InlineKeyboardButton("❗️SELL", callback_data=f"addtag_{hold_id}_❗️SELL")],
        [InlineKeyboardButton("💳 PAYMENT", callback_data=f"addtag_{hold_id}_💳 PAYMENT")],
        [InlineKeyboardButton("📦 ORDER", callback_data=f"addtag_{hold_id}_📦 ORDER")],
        [InlineKeyboardButton("🔙 Back", callback_data=f"hold_{hold_id}")]
    ]

    await query.edit_message_text(
        "Select a tag to add:",
        reply_markup=InlineKeyboardMarkup(keyboard))
    return MANAGE_HOLDS

async def apply_tag(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    _, hold_id, tag = query.data.split('_', 2)
    hold_id = int(hold_id)

    with get_db_connection() as conn:
        # Get current tags
        cursor = conn.cursor()
        cursor.execute("SELECT tags FROM holds WHERE id = ?", (hold_id,))
        result = cursor.fetchone()
        current_tags = result[0] if result and result[0] else ""
        
        # Add new tag if not already present
        if f"[{tag}]" not in current_tags:
            new_tags = f"{current_tags} [{tag}]" if current_tags else f"[{tag}]"
            cursor.execute(
                "UPDATE holds SET tags = ? WHERE id = ?",
                (new_tags, hold_id)
            )
            conn.commit()

            # Get the updated hold to show in message
            cursor.execute("SELECT description FROM holds WHERE id = ?", (hold_id,))
            description = cursor.fetchone()[0] or ""
            
            await query.edit_message_text(
                f"✅ Tag added: [{tag}]\nNew description: {new_tags}{description}",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data=f"hold_{hold_id}")]])
            )
        else:
            await query.edit_message_text(
                f"ℹ️ Tag [{tag}] already exists",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data=f"hold_{hold_id}")]]))
    return MANAGE_HOLDS

async def save_hold_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.message.from_user.id
    new_description = update.message.text
    hold_id = context.user_data.get('editing_hold')

    if not hold_id:
        await update.message.reply_text(
            "Hold not found. Please start over.",
            reply_markup=ReplyKeyboardMarkup(main_keyboard, resize_keyboard=True))
        return MAIN_MENU

    with get_db_connection() as conn:
        # Get current tags to preserve them
        cursor = conn.cursor()
        cursor.execute("SELECT tags FROM holds WHERE id = ?", (hold_id,))
        result = cursor.fetchone()
        tags = result[0] if result and result[0] else ""

        # Update description but keep tags
        cursor.execute(
            "UPDATE holds SET description = ? WHERE id = ? AND user_id = ?",
            (new_description, hold_id, user_id)
        )
        conn.commit()

    await update.message.reply_text(
        f"✅ Hold description updated!\nNew description: {tags}{new_description}",
        reply_markup=ReplyKeyboardMarkup(main_keyboard, resize_keyboard=True))
    
    context.user_data.pop('editing_hold', None)
    return MAIN_MENU

async def transfer_hold(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    action, hold_id = query.data.split('_')[1], int(query.data.split('_')[2])
    user_id = query.from_user.id

    with get_db_connection() as conn:
        hold = conn.execute("SELECT * FROM holds WHERE id = ?", (hold_id,)).fetchone()
        if not hold:
            await query.edit_message_text("Hold not found!")
            return MAIN_MENU
            
        currency = hold['currency']
        wallet_id = hold.get('wallet_id')
        
        # Create transaction
        trans_type = 'income' if action == 'income' else 'outcome'
        amount = hold['amount'] if action == 'income' else -hold['amount']
        description = f"{'Released' if action == 'income' else 'Spent'} hold: {hold['description']}"
        
        # If hold was from a wallet, return funds when transferring to expense
        if wallet_id and action == 'outcome':
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE wallets SET balance = balance + ? WHERE id = ?",
                (hold['amount'], wallet_id)
            )
        
        # Insert transaction
        conn.execute(
            "INSERT INTO transactions (user_id, type, amount, description, date, currency, wallet_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (user_id, trans_type, amount, description, get_current_datetime(), currency, wallet_id)
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
    try:
        # Handle both Message and CallbackQuery updates
        if update.message:
            reply_method = update.message.reply_text
        elif update.callback_query:
            await update.callback_query.answer()
            reply_method = update.callback_query.message.reply_text
        else:
            logger.error("Invalid update received in settings_menu")
            return MAIN_MENU

        await reply_method(
            "⚙️ Settings Menu",
            reply_markup=ReplyKeyboardMarkup(settings_keyboard, resize_keyboard=True))
        return SETTINGS_MENU

    except Exception as e:
        logger.error(f"Error in settings_menu: {str(e)}")
        if update.effective_message:
            await update.effective_message.reply_text(
                "An error occurred. Returning to main menu.",
                reply_markup=ReplyKeyboardMarkup(main_keyboard, resize_keyboard=True))
        return MAIN_MENU

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "Action cancelled",
        reply_markup=ReplyKeyboardMarkup(main_keyboard, resize_keyboard=True))
    return MAIN_MENU

async def show_recent_transactions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM transactions WHERE user_id = ? "
            "ORDER BY date DESC",
            (user_id,)
        )
        transactions = cursor.fetchall()

    if not transactions:
        await query.edit_message_caption(
            caption="No transactions found!",
            reply_markup=None
        )
        return MAIN_MENU

    # Format each transaction with proper styling
    trans_text = "<b>📜 𝗧𝗿𝗮𝗻𝘀𝗮𝗰𝘁𝗶𝗼𝗻 𝗛𝗶𝘀𝘁𝗼𝗿𝘆</b>\n\n"
    
    for t in transactions:
        date = format_transaction_date_long(t['date'])
        trans_type = "🟢 Income" if t['type'] == 'income' else "🔴 Expense"
        amount = t['amount']
        currency = t['currency']
        wallet_name = get_wallet_name(t['wallet_id']) if t['wallet_id'] else "Unknown"
        description = t['description']
        
        # Format amount with color
        amount_str = format_money(abs(amount), currency)
        if amount > 0:
            amount_display = f"<b>+{amount_str}</b>"
        else:
            amount_display = f"<b>–{amount_str}</b>"
        
        trans_text += (
            f"<b>{date}</b>\n"
            f"{trans_type} • {wallet_name}\n"
            f"💸 Amount: {amount_display}\n"
            f"📝 {description}\n\n"
        )
    
    # Add pagination controls
    keyboard = [
        [InlineKeyboardButton("🔙 Back to Summary", callback_data="back_to_summary")],
        #[InlineKeyboardButton("🗑 Delete Transaction", callback_data="delete_transactions")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    # Edit the caption of the existing photo message
    await query.edit_message_caption(
        caption=trans_text,
        parse_mode='HTML',
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
    try:
        logger.error('Update "%s" caused error "%s"', update, context.error, exc_info=context.error)
        
        if update.callback_query:
            try:
                await update.callback_query.answer()
                await update.callback_query.message.reply_text(
                    "An error occurred. Please try again.",
                    reply_markup=ReplyKeyboardMarkup(main_keyboard, resize_keyboard=True)
                )
            except Exception as e:
                logger.error(f"Error handling callback error: {str(e)}")
        elif update.message:
            await update.message.reply_text(
                "An error occurred. Please try again.",
                reply_markup=ReplyKeyboardMarkup(main_keyboard, resize_keyboard=True)
            )
    except Exception as e:
        logger.error(f"Error in error handler: {str(e)}")

def main() -> None:
    # Initialize database
    init_db()

    # Create application with job queue
    try:
        TOKEN = os.getenv("TELEGRAM_TOKEN", "7847351145:AAEgUwSdwpYoLANB06PumVfTeQEH-I85EbM")
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
        CHOOSE_WALLET_INCOME: [
            CallbackQueryHandler(wallet_chosen_income, pattern=r"^income_wallet_"),
            CallbackQueryHandler(start_over, pattern=r"^back_income"),
        ],
        CHOOSE_WALLET_OUTCOME: [
            CallbackQueryHandler(wallet_chosen_outcome, pattern=r"^outcome_wallet_"),
            CallbackQueryHandler(start_over, pattern=r"^back_outcome"),
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
    MessageHandler(filters.Regex(r'^💸 Transfer Funds$'), transfer_funds_prompt),
    MessageHandler(filters.Regex(r'^➕ Add Wallet$'), add_wallet_prompt),
    MessageHandler(filters.Regex(r'^🏷 Set Default Wallet$'), set_default_wallet),
    MessageHandler(filters.Regex(r'^🔙 Back$'), start_over),
    CallbackQueryHandler(handle_set_default, pattern=r"^setdef_"),
    CallbackQueryHandler(wallets_menu, pattern=r"^back_wallets$"),
    CallbackQueryHandler(wallet_actions, pattern=r"^wallet_"),
    CallbackQueryHandler(select_target_wallet, pattern=r"^transfer_from_"),
        ],
        ADD_HOLD_FROM_WALLET: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, process_hold_from_wallet),
            CallbackQueryHandler(wallet_actions, pattern=r"^back_wallet_actions$"),
            ],
            TRANSFER_TARGET: [
    CallbackQueryHandler(enter_transfer_amount, pattern=r"^transfer_to_"),
    CallbackQueryHandler(cancel_transfer, pattern=r"^cancel_transfer$"),
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
    CallbackQueryHandler(back_to_settings, pattern=r"^back_to_settings$"),
    CallbackQueryHandler(settings_menu, pattern=r"^back_currency$")
            ],
            TRANSFER_AMOUNT: [
    MessageHandler(filters.TEXT & ~filters.COMMAND, process_transfer),
    CallbackQueryHandler(cancel_transfer, pattern=r"^cancel_transfer$"),
],
            MANAGE_HOLDS: [
                
                    MessageHandler(filters.Regex(r'^➕ Normal Hold$'), add_hold_prompt),
    MessageHandler(filters.Regex(r'^➕ From Wallet$'), choose_wallet_for_hold),
    MessageHandler(filters.Regex(r'^🛠 Manage Hold$'), manage_hold_menu),
    MessageHandler(filters.Regex(r'^🔙 Back$'), start_over),
    CallbackQueryHandler(hold_action, pattern=r"^hold_"),
    CallbackQueryHandler(transfer_hold, pattern=r"^transfer_(income|outcome)_"),
    CallbackQueryHandler(remove_hold, pattern=r"^remove_"),
    CallbackQueryHandler(start_over, pattern=r"^back_holds"),
    # Add new callback handlers
    CallbackQueryHandler(wallet_chosen_for_hold, pattern=r"^hold_wallet_"),
    CallbackQueryHandler(holds_menu, pattern=r"^back_choose_wallet_hold$"),
    CallbackQueryHandler(rename_hold_prompt, pattern=r"^rename_\d+$"),
        CallbackQueryHandler(add_tag_prompt, pattern=r"^tag_\d+$"),
        CallbackQueryHandler(apply_tag, pattern=r"^addtag_\d+_.+$"),
            ],
            EDIT_HOLD: [
        MessageHandler(filters.TEXT & ~filters.COMMAND, save_hold_name),
        CallbackQueryHandler(hold_action, pattern=r"^hold_\d+$")
    ],
            CHOOSE_WALLET_FOR_HOLD: [
    CallbackQueryHandler(wallet_chosen_for_hold, pattern=r"^hold_wallet_"),
    CallbackQueryHandler(holds_menu, pattern=r"^back_holds$"),
],
            MANAGE_TRANSACTIONS: [
    CallbackQueryHandler(delete_transaction, pattern=r"^del_trans_"),
    CallbackQueryHandler(back_to_transactions, pattern=r"^back_to_transactions$"),
    CallbackQueryHandler(start_over, pattern=r"^back_transactions$")
],
            SET_BUDGET: [
                CallbackQueryHandler(budget_category_selected, pattern=r"^budgetcat_"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, save_budget),
                CallbackQueryHandler(start_over, pattern=r"^cancel_budget$"),
            ],
            REPORT_MENU: [
    CallbackQueryHandler(show_report, pattern=r"^report_"),
    CallbackQueryHandler(settings_menu, pattern=r"^back_to_reports$"),
    MessageHandler(filters.Regex(r'^🔙 Back$'), start_over)
],
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
