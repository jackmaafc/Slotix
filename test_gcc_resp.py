import requests

def mock_gcc_confirm(payment_id, order_id, temp_book_id, amount):
    # This simulates what happens in app.py
    gcc_data = {"status": "SUCCESS"} # Suppose GCC returns this
    if gcc_data and gcc_data != "error":
        booking_id = str(gcc_data)
    else:
        booking_id = None
        
    print(f"booking_id: {booking_id}")
    
mock_gcc_confirm("1", "2", "3", 100)
