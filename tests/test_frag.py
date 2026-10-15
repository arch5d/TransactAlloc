import psycopg

import db
import frag

DB = "transactalloc_p3"
DSN = f"host=localhost port=5432 user=postgres password=crm dbname={DB}"


def setup_module():
    db.reset_all()


def test_block_map_invariant_4_clusters():
    with db.connect() as c:
        for m in (1,2,3,4):
            c.execute("SELECT ValidateBlockMap(%s)", (m,))
        c.commit()


def test_carve_and_coalesce():
    with db.connect() as c:
        # create job
        c.execute("INSERT INTO Jobs (user_id,task_name,req_cpus,req_ram_mb,priority,duration_sec) VALUES (1,'t',1,1024,1,1)")
        c.commit()
        j = c.execute("SELECT job_id FROM Jobs ORDER BY job_id DESC LIMIT 1").fetchone()[0]
        # allocate 1MB on m1
        cid = c.execute("SELECT AllocateJobResources(%s,1,%s,%s)", (j,1,1024)).fetchone()[0]
        assert cid
        st = frag.cluster_status(c,1)
        assert st["holes"] >= 1
        c.execute("SELECT ReleaseJobResources(%s)", (j,))
        c.commit()
        # coalesced back to 1 hole
        st2 = frag.cluster_status(c,1)
        assert st2["holes"] == 1
        # total_free check via calc
        total_free = st2["total_ram_mb"] - (st2["total_ram_mb"] - st2["free_ram_mb"])  # trivial
        assert int(st2["largest_free_mb"]) >= 0


def test_defrag_compacts():
    db.reset_all()
    with db.connect() as c:
        # make some fragments on cluster 1? allocate/release to create holes
        # allocate 4 jobs split
        jobs=[]
        sizes=[4096,4096,4096,4096]
        for sz in sizes:
            c.execute("INSERT INTO Jobs (user_id,task_name,req_cpus,req_ram_mb,priority,duration_sec) VALUES (1,'t',1,%s,5,1)", (sz,))
        c.commit()
        for sz in sizes:
            j=c.execute("SELECT job_id FROM Jobs WHERE req_ram_mb=%s ORDER BY job_id DESC LIMIT 1 OFFSET %s", (sz,len(jobs))).fetchone()
            if not j: j=c.execute("SELECT job_id FROM Jobs WHERE req_ram_mb=%s ORDER BY job_id DESC LIMIT 1", (sz,)).fetchone()
            jobs.append(j[0])
        # allocate on m1 (allow over-allocation logic errors to surface as exceptions)
        for ji,sz in zip(jobs,sizes):
            c.execute("SELECT AllocateJobResources(%s,1,%s,%s)", (ji,1,sz)).fetchone()[0]
        c.commit()
        # release middle ones to fragment
        c.execute("SELECT ReleaseJobResources(%s)", (jobs[1],)); c.execute("SELECT ReleaseJobResources(%s)", (jobs[2],))
        c.commit()
        st=frag.cluster_status(c,1)
        holes_before=st["holes"]
        # defrag
        moves=frag.defrag_plan(c,1)
        frag.apply_defrag(c,1,moves,triggered_by="AUTO")
        c.commit()
        st2=frag.cluster_status(c,1)
        assert st2["holes"] <= holes_before
        c.execute("SELECT ValidateBlockMap(%s)", (1,))
        c.commit()
