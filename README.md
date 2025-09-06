# evsemaster

Python client library for communicating with a EVSE chargers that use the EVSEMaster app. I've only done my testing on a Telestar EC311S6, but it *should* work with any EVSE that uses the EVSEMaster app protocol.   
I'm intenting to keep this a simple implementation, so it does not have all the features of the original TypeScript project, but it should be sufficient for basic use cases like Home Assistant integration.

This is based on the original TypeScript project by [johnwoo-nl](https://github.com/johnwoo-nl/emproto)

## Currently Implemented
- Get EVSE device info
- Get EVSE status
- Get EVSE charging status
- Start/Stop charging
- Get/Set EVSE nickname
- Get/Set Current limit

## Being Implemented
- Create/Update charging schedule
- Getting/Setting device properties like time, language, etc.

## Not Planned
- Home Assistant or MQTT integration (see [evsemaster-homeassistant](https://github.com/RafaelSchridi/evsemaster-homeassistant))
- Connecting to the EVSE via Bluetooth (ie. for connecting the EVSE to wifi)
  * Even though the EVSEMaster app is awful to use, using it once to connect the EVSE to wifi is sufficient for most use cases.


## Usage
```python
from evsemaster.evse_protocol import SimpleEVSEProtocol

def event_callback(event, data):
    print(f"Event: {event}, Data: {data}")

async def main():
    evse = SimpleEVSEProtocol(
        host="10.0.0.1",  # IP address of the EVSE
        password="123456",  # 6 digit password of the EVSE
        callback=event_callback,  # Callback function for events
    )
    await evse.connect()  # Connect to the EVSE
    await evse.request_status() # Request the current status of the EVSE
    await evse.start_charging()  # Start charging
    await evse.stop_charging()  # Stop charging
    await evse.disconnect()  # Disconnect from the EVSE
```

There is a test script `test.py` that can be used to test the library. 
Its a bit messy as it just prints the output while accepting commands, but it can be useful for quick testing.
```bash
poetry run python test.py 10.0.0.1 123456
```
