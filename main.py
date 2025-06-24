import datetime
import logging
import sqlite3
import pytz
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
def get_db_connection():
    conn = sqlite3.connect('budget.db', check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db_connection() as conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS transactions
                     (id INTEGER PRIMARY KEY, user_id INTEGER, type TEXT, amount REAL,
                     description TEXT, date TEXT, currency TEXT)''')
        conn.execute('''CREATE TABLE IF NOT EXISTS holds
                     (id INTEGER PRIMARY KEY, user_id INTEGER, amount REAL, description TEXT, currency TEXT)''')
        conn.execute('''CREATE TABLE IF NOT EXISTS recurring
                     (id INTEGER PRIMARY KEY, user_id INTEGER, type TEXT, amount REAL,
                     description TEXT, currency TEXT, day_of_month INTEGER,
                     is_active INTEGER DEFAULT 1)''')
        conn.execute('''CREATE TABLE IF NOT EXISTS user_settings
                     (user_id INTEGER PRIMARY KEY, default_currency TEXT)''')

# --- Constants --- #
MAIN_MENU, ADD_INCOME, ADD_OUTCOME, ADD_HOLD, MANAGE_HOLDS, \
RECURRING_MENU, ADD_RECURRING, CURRENCY_MENU = range(8)
TIMEZONE = pytz.timezone('Europe/Moscow')
CURRENCIES = {
    'USD': '$',
    'EUR': '€',
    'CHF': 'Fr',
    'GBP': '£',
    'JPY': '¥',
    'RUB': '₽'
}

# Keyboard layouts
main_keyboard = [
    ['💰 Balance', '📥 Income'], 
    ['📤 Outcome', '⏳ Holds'],
    ['🔄 Recurring', '💱 Currency']
]
cancel_keyboard = [['❌ Cancel']]

# --- Logging Setup --- #
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- Helper Functions --- #
def get_user_currency(user_id):
    with get_db_connection() as conn:
        currency = conn.execute("SELECT default_currency FROM user_settings WHERE user_id = ?", 
                              (user_id,)).fetchone()
        return currency['default_currency'] if currency else 'USD'

def format_money(amount, currency='USD'):
    symbol = CURRENCIES.get(currency, '$')
    return f"{symbol}{abs(amount):.2f}"

def get_current_datetime():
    return datetime.datetime.now(TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")

async def process_recurring_transactions(context: ContextTypes.DEFAULT_TYPE):
    today = datetime.datetime.now(TIMEZONE).day
    with get_db_connection() as conn:
        recurring = conn.execute("SELECT * FROM recurring WHERE day_of_month = ? AND is_active = 1", 
                               (today,)).fetchall()
        
        for transaction in recurring:
            conn.execute(
                "INSERT INTO transactions (user_id, type, amount, description, date, currency) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (transaction['user_id'], transaction['type'], transaction['amount'], 
                 transaction['description'], get_current_datetime(), transaction['currency'])
            )
            logger.info(f"Processed recurring transaction for user {transaction['user_id']}")

# --- Handlers --- #
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.message.from_user
    
    with get_db_connection() as conn:
        user_exists = conn.execute("SELECT 1 FROM user_settings WHERE user_id = ?", 
                                 (user.id,)).fetchone()
        if not user_exists:
            conn.execute("INSERT INTO user_settings (user_id, default_currency) VALUES (?, ?)",
                       (user.id, 'USD'))
    
    await update.message.reply_text(
        f"Welcome to Budget Planner, {user.first_name}!\n"
        "Use the buttons below to manage your finances:",
        reply_markup=ReplyKeyboardMarkup(main_keyboard, resize_keyboard=True)
    )
    return MAIN_MENU

async def show_balance(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.message.from_user.id
    currency = get_user_currency(user_id)
    
    with get_db_connection() as conn:
        income = conn.execute("SELECT SUM(amount) FROM transactions WHERE user_id = ? AND type = 'income'", 
                            (user_id,)).fetchone()[0] or 0
        outcome = conn.execute("SELECT SUM(amount) FROM transactions WHERE user_id = ? AND type = 'outcome'", 
                             (user_id,)).fetchone()[0] or 0
        holds = conn.execute("SELECT SUM(amount) FROM holds WHERE user_id = ?", 
                           (user_id,)).fetchone()[0] or 0

    balance = income + outcome
    total_expenses = -outcome if outcome < 0 else outcome

    await update.message.reply_text(
        f"💰 Current Balance: {format_money(balance, currency)}\n"
        f"📥 Total Income: {format_money(income, currency)}\n"
        f"📤 Total Expenses: {format_money(total_expenses, currency)}\n"
        f"⏳ Held Amount: {format_money(holds, currency)}",
        reply_markup=ReplyKeyboardMarkup(main_keyboard, resize_keyboard=True))
    return MAIN_MENU

async def currency_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    keyboard = [
        [InlineKeyboardButton(f"{code} {symbol}", callback_data=f"currency_{code}") 
         for code, symbol in list(CURRENCIES.items())[i:i+2]]
        for i in range(0, len(CURRENCIES), 2)
    ]
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
    
    await query.edit_message_text(
        f"✅ Default currency set to {currency_code} {CURRENCIES.get(currency_code, '')}")
    return await start_over(update, context)

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
    # Store the type in user_data before asking for details
    context.user_data['recurring_type'] = update.message.text.lower()
    await update.message.reply_text(
        "Enter details in format:\n"
        "<b>Amount DayOfMonth Description</b>\n"
        "Example: 1000 5 Salary\n"
        "DayOfMonth should be between 1-28",
        reply_markup=ReplyKeyboardMarkup(cancel_keyboard, resize_keyboard=True),
        parse_mode="HTML")
    return ADD_RECURRING

async def add_recurring(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.message.from_user.id
    text = update.message.text.split()
    currency = get_user_currency(user_id)

    try:
        # Check if recurring_type exists in context
        if 'recurring_type' not in context.user_data:
            await update.message.reply_text(
                "Please start over and select the transaction type first",
                reply_markup=ReplyKeyboardMarkup(cancel_keyboard, resize_keyboard=True))
            return ADD_RECURRING

        amount = float(text[0])
        day = int(text[1])
        description = ' '.join(text[2:]) if len(text) > 2 else "Recurring"
        
        if not 1 <= day <= 28:
            raise ValueError("Day must be between 1-28")

        with get_db_connection() as conn:
            conn.execute(
                "INSERT INTO recurring (user_id, type, amount, description, day_of_month, currency) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (user_id, context.user_data['recurring_type'], amount, description, day, currency)
            )

        # Clear the recurring_type from user_data after successful addition
        context.user_data.pop('recurring_type', None)

        await update.message.reply_text(
            f"✅ Added recurring {context.user_data.get('recurring_type', 'transaction')}: "
            f"{format_money(amount, currency)} on day {day} for {description}",
            reply_markup=ReplyKeyboardMarkup(main_keyboard, resize_keyboard=True))
        return MAIN_MENU

    except (ValueError, IndexError) as e:
        await update.message.reply_text(
            f"❌ Invalid format: {str(e)}\n"
            "Please enter: Amount DayOfMonth Description\n"
            "Example: 1000 5 Salary",
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

    keyboard = [
        [InlineKeyboardButton(
            f"{idx}. {trans['type'].capitalize()}: {format_money(trans['amount'], trans['currency'])} "
            f"on day {trans['day_of_month']} - {trans['description']}",
            callback_data=f"recur_{trans['id']}"
        )]
        for idx, trans in enumerate(recurring, 1)
    ]
    keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="back_recurring")])

    await update.message.reply_text(
        "📋 Your recurring transactions:\nSelect one to manage:",
        reply_markup=InlineKeyboardMarkup(keyboard))
    return RECURRING_MENU

async def manage_recurring(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    recurring_id = query.data.split('_')[1]
    context.user_data['current_recurring'] = recurring_id

    with get_db_connection() as conn:
        recurring = conn.execute("SELECT * FROM recurring WHERE id = ?", 
                              (recurring_id,)).fetchone()

    await query.edit_message_text(
        text=f"Recurring transaction selected:\n"
             f"Type: {recurring['type'].capitalize()}\n"
             f"Amount: {format_money(recurring['amount'], recurring['currency'])}\n"
             f"Day: {recurring['day_of_month']}\n"
             f"Description: {recurring['description']}\n\n"
             "Choose action:",
        reply_markup=InlineKeyboardMarkup([
            #[InlineKeyboardButton("✏️ Edit", callback_data=f"edit_recur_{recurring_id}"),
            [InlineKeyboardButton("❌ Remove", callback_data=f"remove_recur_{recurring_id}")],
            [InlineKeyboardButton("🔙 Back", callback_data="back_recur_list")]
        ]))
    return RECURRING_MENU

async def remove_recurring(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    recurring_id = query.data.split('_')[2]

    with get_db_connection() as conn:
        conn.execute("DELETE FROM recurring WHERE id = ?", (recurring_id,))

    await query.edit_message_text("✅ Recurring transaction removed successfully!")
    return await start_over(update, context)

async def edit_recurring_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    recurring_id = query.data.split('_')[2]
    context.user_data['editing_recurring'] = recurring_id

    # Use inline keyboard instead of reply keyboard
    await query.edit_message_text(
        "Enter new details in format:\n"
        "<b>Amount DayOfMonth Description</b>\n"
        "Example: 1000 5 Salary\n"
        "DayOfMonth should be between 1-28",
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
        
        if not 1 <= day <= 28:
            raise ValueError("Day must be between 1-28")

        with get_db_connection() as conn:
            # Get existing recurring to preserve type and currency
            recurring = conn.execute("SELECT * FROM recurring WHERE id = ?",
                                   (context.user_data['editing_recurring'],)).fetchone()
            
            conn.execute(
                "UPDATE recurring SET amount = ?, day_of_month = ?, description = ? WHERE id = ?",
                (amount, day, description, context.user_data['editing_recurring'])
            )

        # Clean up
        context.user_data.pop('editing_recurring', None)

        # Use inline keyboard for consistency
        keyboard = [
            [InlineKeyboardButton("🔙 Back to Main Menu", callback_data="back_main")]
        ]
        
        await update.message.reply_text(
            f"✅ Updated recurring transaction:\n"
            f"Amount: {format_money(amount, recurring['currency'])}\n"
            f"Day: {day}\n"
            f"Description: {description}",
            reply_markup=InlineKeyboardMarkup(keyboard))
        return MAIN_MENU

    except (ValueError, IndexError) as e:
        keyboard = [
            [InlineKeyboardButton("❌ Cancel", callback_data="back_recur_list")]
        ]
        await update.message.reply_text(
            f"❌ Invalid format: {str(e)}\n"
            "Please enter: Amount DayOfMonth Description\n"
            "Example: 1000 5 Salary",
            reply_markup=InlineKeyboardMarkup(keyboard))
        return ADD_RECURRING

async def income_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.message.from_user.id
    currency = get_user_currency(user_id)
    
    await update.message.reply_text(
        f"➕ Add income in format (currency: {currency}):\n"
        "<b>Amount</b> (e.g., 1000)\n"
        "OR\n"
        "<b>Amount Description</b> (e.g., 1000 Salary)",
        reply_markup=ReplyKeyboardMarkup(cancel_keyboard, resize_keyboard=True),
        parse_mode="HTML")
    return ADD_INCOME

async def add_income(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.message.from_user.id
    text = update.message.text.split()
    currency = get_user_currency(user_id)

    try:
        amount = float(text[0])
        description = ' '.join(text[1:]) if len(text) > 1 else "Income"

        with get_db_connection() as conn:
            conn.execute(
                "INSERT INTO transactions (user_id, type, amount, description, date, currency) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (user_id, 'income', amount, description, get_current_datetime(), currency)
            )

        await update.message.reply_text(
            f"✅ Added income: {format_money(amount, currency)} for {description}",
            reply_markup=ReplyKeyboardMarkup(main_keyboard, resize_keyboard=True))
        return MAIN_MENU

    except (ValueError, IndexError):
        await update.message.reply_text(
            "❌ Invalid format. Please enter amount and optional description",
            reply_markup=ReplyKeyboardMarkup(cancel_keyboard, resize_keyboard=True))
        return ADD_INCOME

async def outcome_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.message.from_user.id
    currency = get_user_currency(user_id)
    
    await update.message.reply_text(
        f"➖ Add expense in format (currency: {currency}):\n"
        "<b>Amount</b> (e.g., 50)\n"
        "OR\n"
        "<b>Amount Description</b> (e.g., 50 Groceries)",
        reply_markup=ReplyKeyboardMarkup(cancel_keyboard, resize_keyboard=True),
        parse_mode="HTML")
    return ADD_OUTCOME

async def add_outcome(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.message.from_user.id
    text = update.message.text.split()
    currency = get_user_currency(user_id)

    try:
        amount = float(text[0]) * -1  # Store as negative
        description = ' '.join(text[1:]) if len(text) > 1 else "Expense"

        with get_db_connection() as conn:
            conn.execute(
                "INSERT INTO transactions (user_id, type, amount, description, date, currency) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (user_id, 'outcome', amount, description, get_current_datetime(), currency)
            )

        await update.message.reply_text(
            f"✅ Added expense: {format_money(-amount, currency)} for {description}",
            reply_markup=ReplyKeyboardMarkup(main_keyboard, resize_keyboard=True))
        return MAIN_MENU

    except (ValueError, IndexError):
        await update.message.reply_text(
            "❌ Invalid format. Please enter amount and optional description",
            reply_markup=ReplyKeyboardMarkup(cancel_keyboard, resize_keyboard=True))
        return ADD_OUTCOME

async def holds_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.message.from_user.id
    currency = get_user_currency(user_id)
    
    with get_db_connection() as conn:
        holds = conn.execute("SELECT * FROM holds WHERE user_id = ?", (user_id,)).fetchall()

    if not holds:
        await update.message.reply_text(
            f"No holds found! Add a new hold (currency: {currency}):",
            reply_markup=ReplyKeyboardMarkup([['➕ Add Hold'], ['🔙 Back']], resize_keyboard=True))
        return MAIN_MENU

    holds_list = "\n".join(
        f"{idx+1}. {format_money(hold['amount'], hold.get('currency', currency))} - {hold['description']}" 
        for idx, hold in enumerate(holds)
    )

    await update.message.reply_text(
        f"⏳ Your holds:\n{holds_list}\n\nSelect an action:",
        reply_markup=ReplyKeyboardMarkup(
            [['➕ Add Hold'], ['🛠 Manage Hold'], ['🔙 Back']], 
            resize_keyboard=True
        )
    )
    return MAIN_MENU

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

        await update.message.reply_text(
            f"⏳ Added hold: {format_money(amount, currency)} for {description}",
            reply_markup=ReplyKeyboardMarkup(main_keyboard, resize_keyboard=True))
        return MAIN_MENU

    except (ValueError, IndexError):
        await update.message.reply_text(
            "❌ Invalid format. Please enter amount and optional description",
            reply_markup=ReplyKeyboardMarkup(cancel_keyboard, resize_keyboard=True))
        return ADD_HOLD

async def holds_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.message.from_user.id
    currency = get_user_currency(user_id)
    
    with get_db_connection() as conn:
        holds = conn.execute("SELECT * FROM holds WHERE user_id = ?", (user_id,)).fetchall()

    if not holds:
        await update.message.reply_text(
            f"No holds found! Add a new hold (currency: {currency}):",
            reply_markup=ReplyKeyboardMarkup([['➕ Add Hold'], ['🔙 Back']], resize_keyboard=True))
        return MAIN_MENU

    holds_list = "\n".join(
        f"{idx+1}. {format_money(hold['amount'], currency)} - {hold['description']}" 
        for idx, hold in enumerate(holds)
    )

    await update.message.reply_text(
        f"⏳ Your holds:\n{holds_list}\n\nSelect an action:",
        reply_markup=ReplyKeyboardMarkup(
            [['➕ Add Hold'], ['🛠 Manage Hold'], ['🔙 Back']], 
            resize_keyboard=True
        )
    )
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
            f"{format_money(hold['amount'], hold['currency'] if 'currency' in hold.keys() else 'USD')} - {hold['description']}", 
            callback_data=f"hold_{hold['id']}"
        )] 
        for hold in holds
    ]

    await update.message.reply_text(
        "Select a hold to manage:",
        reply_markup=InlineKeyboardMarkup(keyboard))
    return MANAGE_HOLDS

async def hold_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    hold_id = query.data.split('_')[1]
    context.user_data['current_hold'] = hold_id

    with get_db_connection() as conn:
        hold = conn.execute("SELECT * FROM holds WHERE id = ?", (hold_id,)).fetchone()

    currency = hold['currency'] if 'currency' in hold.keys() else 'USD'
    
    await query.edit_message_text(
        text=f"Hold selected: {format_money(hold['amount'], currency)} - {hold['description']}\nChoose action:",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("➡️ To Income", callback_data=f"transfer_income_{hold_id}"),
             InlineKeyboardButton("⬅️ To Outcome", callback_data=f"transfer_outcome_{hold_id}")],
            [InlineKeyboardButton("✏️ Edit", callback_data=f"edit_{hold_id}"),
             InlineKeyboardButton("❌ Remove", callback_data=f"remove_{hold_id}")],
            [InlineKeyboardButton("🔙 Back", callback_data="back_holds")]
        ]))
    return MANAGE_HOLDS

async def transfer_hold(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    action, hold_id = query.data.split('_')[1], query.data.split('_')[2]
    user_id = query.from_user.id

    with get_db_connection() as conn:
        hold = conn.execute("SELECT * FROM holds WHERE id = ?", (hold_id,)).fetchone()
        currency = hold['currency'] if 'currency' in hold.keys() else 'USD'
        
        # Add to transactions
        transaction_type = 'income' if action == 'income' else 'outcome'
        sign = 1 if action == 'income' else -1
        conn.execute(
            "INSERT INTO transactions (user_id, type, amount, description, date, currency) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, transaction_type, sign * hold['amount'], 
             f"From hold: {hold['description']}", get_current_datetime(), currency)
        )
        
        # Remove hold
        conn.execute("DELETE FROM holds WHERE id = ?", (hold_id,))

    await query.edit_message_text(
        f"✅ Transferred {format_money(hold['amount'], currency)} to {transaction_type.capitalize()}")
    return await start_over(update, context)

async def remove_hold(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    hold_id = query.data.split('_')[1]

    with get_db_connection() as conn:
        conn.execute("DELETE FROM holds WHERE id = ?", (hold_id,))

    await query.edit_message_text("✅ Hold removed successfully!")
    return await start_over(update, context)

async def start_over(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    reply_text = "Back to main menu:"
    reply_markup = ReplyKeyboardMarkup(main_keyboard, resize_keyboard=True)
    
    if update.callback_query:
        await update.callback_query.message.reply_text(reply_text, reply_markup=reply_markup)
    else:
        await update.message.reply_text(reply_text, reply_markup=reply_markup)
    return MAIN_MENU

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "Action cancelled",
        reply_markup=ReplyKeyboardMarkup(main_keyboard, resize_keyboard=True))
    return MAIN_MENU

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
    application = Application.builder().token("8017763140:AAG9PeLy2ktLG5Q6ZGjTI7B8nk7eHVSxemw").build()
    application.add_error_handler(error_handler)

    # Add job queue for recurring transactions
    job_queue = application.job_queue
    if job_queue:
        job_queue.run_daily(process_recurring_transactions, 
                          time=datetime.time(hour=0, minute=0, tzinfo=TIMEZONE))

    # Conversation handler
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler('start', start)],
        states={
            MAIN_MENU: [
                MessageHandler(filters.Regex(r'^💰 Balance$'), show_balance),
                MessageHandler(filters.Regex(r'^📥 Income$'), income_menu),
                MessageHandler(filters.Regex(r'^📤 Outcome$'), outcome_menu),
                MessageHandler(filters.Regex(r'^⏳ Holds$'), holds_menu),
                MessageHandler(filters.Regex(r'^🔄 Recurring$'), recurring_menu),
                MessageHandler(filters.Regex(r'^💱 Currency$'), currency_menu),
                MessageHandler(filters.Regex(r'^➕ Add Hold$'), add_hold_prompt),
                MessageHandler(filters.Regex(r'^🛠 Manage Hold$'), manage_hold_menu),
                MessageHandler(filters.Regex(r'^📋 List Recurring$'), list_recurring),
                MessageHandler(filters.Regex(r'^🔙 Back$'), start),
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
    CallbackQueryHandler(manage_recurring, pattern=r"^recur_"),
    CallbackQueryHandler(remove_recurring, pattern=r"^remove_recur_"),
    CallbackQueryHandler(edit_recurring_prompt, pattern=r"^edit_recur_"),
    CallbackQueryHandler(start_over, pattern=r"^back_recur_list"),
    CallbackQueryHandler(start_over, pattern=r"^back_main"),  # Add this new handler
    MessageHandler(filters.Regex(r'^🔙 Back$'), start),
            ],
            ADD_RECURRING: [
                MessageHandler(filters.Regex(r'^(Income|Outcome)$'), add_recurring_type),
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_recurring),
                MessageHandler(filters.Regex(r'^❌ Cancel$'), cancel)
            ],
            CURRENCY_MENU: [
                CallbackQueryHandler(set_currency, pattern=r"^currency_"),
                CallbackQueryHandler(start_over, pattern=r"^back_currency")
            ],
            MANAGE_HOLDS: [
                CallbackQueryHandler(hold_action, pattern=r"^hold_"),
                CallbackQueryHandler(transfer_hold, pattern=r"^transfer_(income|outcome)_"),
                CallbackQueryHandler(remove_hold, pattern=r"^remove_"),
                CallbackQueryHandler(start_over, pattern=r"^back_holds")
            ]
        },
        fallbacks=[CommandHandler('start', start)],
        allow_reentry=True
    )

    application.add_handler(conv_handler)
    
    # Start the Bot
    application.run_polling()

if __name__ == '__main__':
    main()
