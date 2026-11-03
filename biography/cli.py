import argparse
from pathlib import Path
import json
from .config import settings
from .db import init_db

def main():
    parser=argparse.ArgumentParser(description='Local personal-history archive')
    sub=parser.add_subparsers(dest='command',required=True)
    sub.add_parser('init')
    sub.add_parser('plan')
    run=sub.add_parser('import')
    run.add_argument('--source',action='append',help='Exact source category; repeat to select several')
    run.add_argument('--move',action='store_true',help='Delete only verified Windows source copies after canonical retention')
    serve=sub.add_parser('serve');serve.add_argument('--port',type=int,default=8000)
    sub.add_parser('status')
    rebuild=sub.add_parser('reindex');rebuild.add_argument('--source')
    sub.add_parser('recover')
    enrich=sub.add_parser('enrich');enrich.add_argument('--limit',type=int,default=10)
    snapshot=sub.add_parser('snapshot',help='Export private records and manifests; original media is excluded')
    snapshot.add_argument('destination',type=Path)
    restore=sub.add_parser('restore',help='Verify a private snapshot and create a new catalog')
    restore.add_argument('snapshot',type=Path);restore.add_argument('database',type=Path)
    restore.add_argument('--vaultroot',type=Path,required=True)
    args=parser.parse_args()
    init_db()
    if args.command=='init':print(json.dumps({'database':str(settings.database)}))
    elif args.command in ('plan','import'):
        from .pipeline import dataset_inventory,register_queue,run_dataset,worker_lock
        queue=register_queue(dataset_inventory())
        if args.command=='plan':print(json.dumps(queue,indent=2));return
        if args.source:
            selected=set(args.source);queue=[d for d in queue if d['name'] in selected]
            missing=selected-{d['name'] for d in queue}
            if missing:parser.error('No remaining export files for: '+', '.join(sorted(missing)))
        with worker_lock():
            for dataset in queue:
                print(json.dumps({'phase':'starting','source':dataset['name'],'bytes':dataset['bytes'],'files':dataset['files']}),flush=True)
                result=run_dataset(dataset,move=args.move)
                print(json.dumps(result),flush=True)
    elif args.command=='serve':
        import uvicorn
        uvicorn.run('biography.api:app',host='127.0.0.1',port=args.port,access_log=False)
    elif args.command=='status':
        from .queries import overview,sources,imports
        print(json.dumps({'overview':overview(),'sources':sources(),'imports':imports()},indent=2))
    elif args.command=='reindex':
        from .pipeline import worker_lock,reindex
        with worker_lock():print(json.dumps(reindex(args.source),indent=2))
    elif args.command=='recover':
        from .pipeline import worker_lock,replay_manifests
        with worker_lock():print(json.dumps(replay_manifests(),indent=2))
    elif args.command=='enrich':
        from .pipeline import worker_lock,enrich_pending
        with worker_lock():print(json.dumps(enrich_pending(args.limit),indent=2))
    elif args.command=='snapshot':
        from .pipeline import worker_lock
        from .snapshots import export_snapshot
        with worker_lock():
            result=export_snapshot(settings.database,args.destination,manifests_dir=settings.data_dir/'manifests')
        print(json.dumps({'destination':str(args.destination),'raw_objects_backed_up':False,'tables':result.get('table_counts')},indent=2))
    elif args.command=='restore':
        from .snapshots import restore_snapshot
        print(json.dumps(restore_snapshot(args.snapshot,args.database,vaultroot=args.vaultroot),indent=2))

if __name__=='__main__':main()
