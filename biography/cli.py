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
    daily=sub.add_parser('daily-build',help='Build or resume the day index')
    daily.add_argument('--rebuild',action='store_true')
    daily.add_argument('--max-seconds',type=float,default=300)
    daily.add_argument('--batch-size',type=int,default=2000)
    sub.add_parser('daily-status',help='Show day index progress')
    media=sub.add_parser('media-build',help='Discover and link media in bounded, resumable batches')
    media.add_argument('--max-seconds',type=float,default=30)
    media.add_argument('--batch-size',type=int,default=500)
    sub.add_parser('media-status',help='Show media discovery and linking progress')
    backup=sub.add_parser('backup',help='Retain a fixed catalog, original files and completed import manifests')
    backup.add_argument('destination',type=Path)
    verify=sub.add_parser('backup-verify',help='Read back and verify a backup')
    verify.add_argument('backup',type=Path)
    full_restore=sub.add_parser('backup-restore',help='Restore a backup into a new directory')
    full_restore.add_argument('backup',type=Path)
    full_restore.add_argument('destination',type=Path)
    args=parser.parse_args()
    if args.command not in {'backup','backup-verify','backup-restore','daily-build','daily-status','media-status','restore'}:
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
        print(json.dumps({'destination':str(args.destination),'raw_objects_backed_up':False,'tables':{name:entry['rows'] for name,entry in result['tables'].items()}},indent=2))
    elif args.command=='restore':
        from .snapshots import restore_snapshot
        print(json.dumps(restore_snapshot(args.snapshot,args.database,vaultroot=args.vaultroot),indent=2))
    elif args.command=='daily-build':
        from .daily import build_daily
        if args.max_seconds<=0:parser.error('--max-seconds must be positive')
        result=build_daily(settings.database,settings.daily_database,rebuild=args.rebuild,
                           batch_size=args.batch_size,max_seconds=args.max_seconds,
                           capacity_paths=settings.capacity_paths,reserve_bytes=settings.reserve_bytes)
        print(json.dumps(result,indent=2))
    elif args.command=='daily-status':
        from .daily import daily_status
        print(json.dumps(daily_status(settings.database,settings.daily_database),indent=2))
    elif args.command=='media-build':
        from .media import backfill_media_candidates,reconcile_media
        if args.max_seconds<=0 or not 1<=args.batch_size<=800:
            parser.error('Choose a positive duration and a batch size from 1 to 800.')
        discovery=backfill_media_candidates(settings.database,batch_size=args.batch_size,
                                           max_batches=100,max_seconds=args.max_seconds)
        linked=reconcile_media(settings.database,index_files=False,limit=args.batch_size)
        print(json.dumps({'discovery':discovery,'linking':linked},indent=2))
    elif args.command=='media-status':
        from .media import media_candidate_status
        print(json.dumps(media_candidate_status(settings.database),indent=2))
    elif args.command=='backup':
        from .backups import create_backup
        result=create_backup(settings.database,settings.data_dir,args.destination,
                             manifests_dir=settings.data_dir/'manifests',
                             reserve_bytes=settings.reserve_bytes,capacity_paths=settings.capacity_paths)
        print(json.dumps(result,indent=2))
    elif args.command=='backup-verify':
        from .backups import verify_backup
        print(json.dumps(verify_backup(args.backup),indent=2))
    elif args.command=='backup-restore':
        from .backups import restore_backup
        result=restore_backup(args.backup,args.destination,reserve_bytes=settings.reserve_bytes,
                              capacity_paths=settings.capacity_paths)
        print(json.dumps(result,indent=2))

if __name__=='__main__':main()
