import logging
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
    filters
)
import sqlite3
from datetime import datetime
import pytz  # Required for timezone handling

# Database setup
conn = sqlite3.connect('budget.db', check_same_thread=False)
c = conn.cursor()
c.execute('''CREATE TABLE IF NOT EXISTS transactions
             (id INTEGER PRIMARY KEY, user_id INTEGER, type TEXT, amount REAL, 
             description TEXT, date TEXT)''')
c.execute('''CREATE TABLE IF NOT EXISTS holds
             (id INTEGER PRIMARY KEY, user_id INTEGER, amount REAL, description TEXT)''')
conn.commit()

# Conversation states
MAIN_MENU, ADD_INCOME, ADD_OUTCOME, ADD_HOLD, MANAGE_HOLDS = range(5)

# Keyboard layouts
main_keyboard = [['💰 Balance', '📥 Income'], ['📤 Outcome', '⏳ Holds']]
cancel_keyboard = [['❌ Cancel']]

# Enable logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.message.from_user
    await update.message.reply_text(
        f"Welcome to Budget Planner, {user.first_name}!\n"
        "Use the buttons below to manage your finances:",
        reply_markup=ReplyKeyboardMarkup(main_keyboard, resize_keyboard=True)
    )
    return MAIN_MENU

async def show_balance(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.message.from_user.id
    # Calculate balance
    c.execute("SELECT SUM(amount) FROM transactions WHERE user_id = ? AND type = 'income'", (user_id,))
    income = c.fetchone()[0] or 0
    c.execute("SELECT SUM(amount) FROM transactions WHERE user_id = ? AND type = 'outcome'", (user_id,))
    outcome = c.fetchone()[0] or 0
    c.execute("SELECT SUM(amount) FROM holds WHERE user_id = ?", (user_id,))
    holds = c.fetchone()[0] or 0
    
    # FIX: Change subtraction to addition since outcome is already negative
    balance = income + outcome
    
    # Display expenses as positive number
    total_expenses = -outcome if outcome < 0 else outcome
    
    await update.message.reply_text(
        f"💰 Current Balance: ${balance:.2f}\n"
        f"📥 Total Income: ${income:.2f}\n"
        f"📤 Total Expenses: ${total_expenses:.2f}\n"
        f"⏳ Held Amount: ${holds:.2f}",
        reply_markup=ReplyKeyboardMarkup(main_keyboard, resize_keyboard=True))
    return MAIN_MENU

async def income_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "➕ Add income in format:\n"
        "<b>Amount</b> (e.g., 1000)\n"
        "OR\n"
        "<b>Amount Description</b> (e.g., 1000 Salary)",
        reply_markup=ReplyKeyboardMarkup(cancel_keyboard, resize_keyboard=True),
        parse_mode="HTML")
    return ADD_INCOME

async def add_income(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.message.from_user.id
    text = update.message.text.split()
    
    try:
        amount = float(text[0])
        description = ' '.join(text[1:]) if len(text) > 1 else "Income"
        
        c.execute("INSERT INTO transactions (user_id, type, amount, description, date) VALUES (?, ?, ?, ?, ?)",
                  (user_id, 'income', amount, description, datetime.now(pytz.utc).strftime("%Y-%m-%d %H:%M:%S")))
        conn.commit()
        
        await update.message.reply_text(
            f"✅ Added income: ${amount:.2f} for {description}",
            reply_markup=ReplyKeyboardMarkup(main_keyboard, resize_keyboard=True))
        return MAIN_MENU
            
    except (ValueError, IndexError):
        await update.message.reply_text(
            "❌ Invalid format. Please enter amount and optional description",
            reply_markup=ReplyKeyboardMarkup(cancel_keyboard, resize_keyboard=True))
        return ADD_INCOME

async def outcome_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "➖ Add expense in format:\n"
        "<b>Amount</b> (e.g., 50)\n"
        "OR\n"
        "<b>Amount Description</b> (e.g., 50 Groceries)",
        reply_markup=ReplyKeyboardMarkup(cancel_keyboard, resize_keyboard=True),
        parse_mode="HTML")
    return ADD_OUTCOME

async def add_outcome(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.message.from_user.id
    text = update.message.text.split()
    
    try:
        amount = float(text[0]) * -1  # Store as negative
        description = ' '.join(text[1:]) if len(text) > 1 else "Expense"
        
        c.execute("INSERT INTO transactions (user_id, type, amount, description, date) VALUES (?, ?, ?, ?, ?)",
                  (user_id, 'outcome', amount, description, datetime.now(pytz.utc).strftime("%Y-%m-%d %H:%M:%S")))
        conn.commit()
        
        await update.message.reply_text(
            f"✅ Added expense: ${-amount:.2f} for {description}",
            reply_markup=ReplyKeyboardMarkup(main_keyboard, resize_keyboard=True))
        return MAIN_MENU
            
    except (ValueError, IndexError):
        await update.message.reply_text(
            "❌ Invalid format. Please enter amount and optional description",
            reply_markup=ReplyKeyboardMarkup(cancel_keyboard, resize_keyboard=True))
        return ADD_OUTCOME

async def holds_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.message.from_user.id
    c.execute("SELECT * FROM holds WHERE user_id = ?", (user_id,))
    holds = c.fetchall()
    
    if not holds:
        await update.message.reply_text("No holds found! Add a new hold:",
            reply_markup=ReplyKeyboardMarkup([['➕ Add Hold'], ['🔙 Back']], resize_keyboard=True))
        return MAIN_MENU
    
    holds_list = "\n".join([f"{idx+1}. ${hold[2]:.2f} - {hold[3]}" for idx, hold in enumerate(holds)])
    context.user_data['holds'] = holds
    
    await update.message.reply_text(
        f"⏳ Your holds:\n{holds_list}\n\n"
        "Select an action:",
        reply_markup=ReplyKeyboardMarkup([['➕ Add Hold'], ['🛠 Manage Hold'], ['🔙 Back']], resize_keyboard=True))
    return MAIN_MENU

async def add_hold_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "⏳ Add hold in format:\n"
        "<b>Amount</b> (e.g., 1000)\n"
        "OR\n"
        "<b>Amount Description</b> (e.g., 1000 Amazon)",
        reply_markup=ReplyKeyboardMarkup(cancel_keyboard, resize_keyboard=True),
        parse_mode="HTML")
    return ADD_HOLD

async def add_hold(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.message.from_user.id
    text = update.message.text.split()
    
    try:
        amount = float(text[0])
        description = ' '.join(text[1:]) if len(text) > 1 else "Hold"
        
        c.execute("INSERT INTO holds (user_id, amount, description) VALUES (?, ?, ?)",
                  (user_id, amount, description))
        conn.commit()
        
        await update.message.reply_text(
            f"⏳ Added hold: ${amount:.2f} for {description}",
            reply_markup=ReplyKeyboardMarkup(main_keyboard, resize_keyboard=True))
        return MAIN_MENU
            
    except (ValueError, IndexError):
        await update.message.reply_text(
            "❌ Invalid format. Please enter amount and optional description",
            reply_markup=ReplyKeyboardMarkup(cancel_keyboard, resize_keyboard=True))
        return ADD_HOLD

async def manage_hold_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.message.from_user.id
    c.execute("SELECT * FROM holds WHERE user_id = ?", (user_id,))
    holds = c.fetchall()
    
    if not holds:
        await update.message.reply_text("No holds available!", 
            reply_markup=ReplyKeyboardMarkup(main_keyboard, resize_keyboard=True))
        return MAIN_MENU
    
    keyboard = []
    for hold in holds:
        keyboard.append([InlineKeyboardButton(
            f"${hold[2]:.2f} - {hold[3]}", 
            callback_data=f"hold_{hold[0]}")])
    
    await update.message.reply_text(
        "Select a hold to manage:",
        reply_markup=InlineKeyboardMarkup(keyboard))
    return MANAGE_HOLDS

async def hold_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    hold_id = query.data.split('_')[1]
    context.user_data['current_hold'] = hold_id
    
    # Get hold details
    c.execute("SELECT * FROM holds WHERE id = ?", (hold_id,))
    hold = c.fetchone()
    
    await query.edit_message_text(
        text=f"Hold selected: ${hold[2]:.2f} - {hold[3]}\nChoose action:",
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
    
    # Get hold details
    c.execute("SELECT * FROM holds WHERE id = ?", (hold_id,))
    hold = c.fetchone()
    
    # Add to transactions
    transaction_type = 'income' if action == 'income' else 'outcome'
    sign = 1 if action == 'income' else -1
    c.execute("INSERT INTO transactions (user_id, type, amount, description, date) VALUES (?, ?, ?, ?, ?)",
              (user_id, transaction_type, sign * hold[2], f"From hold: {hold[3]}", datetime.now(pytz.utc).strftime("%Y-%m-%d %H:%M:%S")))
    
    # Remove hold
    c.execute("DELETE FROM holds WHERE id = ?", (hold_id,))
    conn.commit()
    
    await query.edit_message_text(
        f"✅ Transferred ${hold[2]:.2f} to {transaction_type.capitalize()}")
    return await start_over(update, context)

async def remove_hold(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    hold_id = query.data.split('_')[1]
    
    c.execute("DELETE FROM holds WHERE id = ?", (hold_id,))
    conn.commit()
    
    await query.edit_message_text("✅ Hold removed successfully!")
    return await start_over(update, context)

async def start_over(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.callback_query:
        await update.callback_query.message.reply_text(
            "Back to main menu:",
            reply_markup=ReplyKeyboardMarkup(main_keyboard, resize_keyboard=True))
    else:
        await update.message.reply_text(
            "Back to main menu:",
            reply_markup=ReplyKeyboardMarkup(main_keyboard, resize_keyboard=True))
    return MAIN_MENU

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "Action cancelled",
        reply_markup=ReplyKeyboardMarkup(main_keyboard, resize_keyboard=True))
    return MAIN_MENU

def main() -> None:
    # Replace with your actual bot token
    token = "8017763140:AAG9PeLy2ktLG5Q6ZGjTI7B8nk7eHVSxemw"
    
    # Create application with job queue disabled
    application = Application.builder().token(token).job_queue(None).build()
    
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler('start', start)],
        states={
            MAIN_MENU: [
                MessageHandler(filters.Regex(r'^💰 Balance$'), show_balance),
                MessageHandler(filters.Regex(r'^📥 Income$'), income_menu),
                MessageHandler(filters.Regex(r'^📤 Outcome$'), outcome_menu),
                MessageHandler(filters.Regex(r'^⏳ Holds$'), holds_menu),
                MessageHandler(filters.Regex(r'^➕ Add Hold$'), add_hold_prompt),
                MessageHandler(filters.Regex(r'^🛠 Manage Hold$'), manage_hold_menu),
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
            MANAGE_HOLDS: [
                CallbackQueryHandler(hold_action, pattern=r"^hold_"),
                CallbackQueryHandler(transfer_hold, pattern=r"^transfer_(income|outcome)_"),
                CallbackQueryHandler(remove_hold, pattern=r"^remove_"),
                CallbackQueryHandler(start_over, pattern=r"^back_")
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
