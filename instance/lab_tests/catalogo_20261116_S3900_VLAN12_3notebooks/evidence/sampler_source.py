"""Non-invasive Linux Ethernet and Wi-Fi counter sampling for lab evidence."""
import csv, time, datetime, os
from pathlib import Path
root=Path('/home/ar7hur/Projetos/quidway-manager-s3900/instance/lab_tests')
out=root/'fedora_network_samples.csv'
def counts(interface):
    base=Path('/sys/class/net')/interface/'statistics'
    return (int((base/'rx_bytes').read_text()),int((base/'tx_bytes').read_text()))
fd=os.open(out,os.O_CREAT|os.O_WRONLY|os.O_TRUNC,0o600)
with os.fdopen(fd,'w',newline='',encoding='utf-8') as stream:
    w=csv.writer(stream)
    w.writerow(['utc_timestamp','elapsed_seconds','eth_rx_bytes','eth_tx_bytes','eth_rx_bps_interval','eth_tx_bps_interval','wifi_rx_bytes','wifi_tx_bytes','wifi_rx_bps_interval','wifi_tx_bps_interval'])
    prev=None
    start=time.monotonic()
    for i in range(19):
        currtime=time.monotonic()
        e=counts('enp0s31f6')
        wifi=counts('wlp2s0')
        cur=(currtime,)+e+wifi
        dt=currtime-prev[0] if prev else 0
        speeds=[round((cur[j]-prev[j])*8/dt) for j in range(1,5)] if prev and dt>0 else [None]*4
        w.writerow([datetime.datetime.now(datetime.timezone.utc).isoformat(),round(currtime-start,2),e[0],e[1],speeds[0],speeds[1],wifi[0],wifi[1],speeds[2],speeds[3]])
        stream.flush()
        prev=cur
        if i<18:time.sleep(10)
print('COMPLETED SAMPLES:',out, 'rows=19')
