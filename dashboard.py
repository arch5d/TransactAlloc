"""Report section 5 -- the live cluster dashboard.

Reads current state straight from the database, so what it prints is the
committed truth rather than anything the scheduler believes in memory.
"""
import sys

BAR_WIDTH = 40
FULL, EMPTY = "█", "░"
RULE = "=" * 80
THIN = "-" * 80

# The block characters are outside cp1252, which is still the default console
# encoding on plenty of Windows machines. One stdlib line beats an ASCII flag.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def bar(percent, width=BAR_WIDTH):
    filled = int(round(percent / 100.0 * width))
    filled = max(0, min(width, filled))
    return FULL * filled + EMPTY * (width - filled)


def banner(title):
    print(RULE)
    print(title)
    print(RULE)


def stamp():
    import time
    return time.strftime("%H:%M:%S")


def render(conn, title="CLOUD RESOURCE MANAGER DASHBOARD — NODE METRICS"):
    banner(title)
    nodes = conn.execute("""
        SELECT m.machine_id, m.hostname, HOST(m.ip_address), m.status,
               c.allocated_cores, c.total_cores,
               r.allocated_ram_mb, r.total_ram_mb
        FROM Machines m
        JOIN CPU_Resources    c ON c.machine_id = m.machine_id
        JOIN Memory_Resources r ON r.machine_id = m.machine_id
        ORDER BY m.machine_id
    """).fetchall()

    for index, (mid, host, ip, status, used_c, total_c, used_r, total_r) in enumerate(nodes):
        cpu_pct = 100.0 * used_c / total_c
        ram_pct = 100.0 * used_r / total_r
        print(f"[NODE {mid:02d}] {host} (IP: {ip}) — STATUS: {status}")
        print(f"CPU Usage: [{bar(cpu_pct)}] {cpu_pct:5.1f}% ({used_c}/{total_c} Cores)")
        print(f"RAM Usage: [{bar(ram_pct)}] {ram_pct:5.1f}% "
              f"({used_r / 1024:.1f}/{total_r / 1024:.1f} GB)")

        active = conn.execute("""
            SELECT job_id FROM Allocations
            WHERE machine_id = %s AND released_at IS NULL
            ORDER BY job_id
        """, (mid,)).fetchall()
        listing = ", ".join(f"Job#{job_id}" for (job_id,) in active) or "(idle)"
        print(f"Active Jobs: {listing}")

        if index < len(nodes) - 1:
            print(THIN)

    print(RULE)


def job_summary(conn):
    counts = conn.execute(
        "SELECT status, COUNT(*) FROM Jobs GROUP BY status ORDER BY status").fetchall()
    print("Jobs: " + (", ".join(f"{status}={n}" for status, n in counts) or "none"))
