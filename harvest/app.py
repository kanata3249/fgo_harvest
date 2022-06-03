import io
import os
import random
import time
from datetime import date, datetime
from logging import getLogger
from typing import Sequence

import boto3  # type: ignore
import botocore.exceptions  # type: ignore
from chalice import (  # type: ignore
    BadRequestError,
    Chalice,
    CORSConfig,
    Rate,
)

from chalicelib import settings
from chalicelib import static
from chalicelib import storage
from chalicelib import twitter
from chalicelib import recording


logger = getLogger()
logger.setLevel(os.environ.get('LOG_LEVEL', 'INFO'))

app = Chalice(app_name='harvest')
app.log.setLevel(os.environ.get('LOG_LEVEL', 'INFO'))
cloudfront = boto3.client('cloudfront')

cors_config = CORSConfig(
    allow_origin=settings.CORSAllowOrigin,
    allow_headers=['X-Special-Header'],
    max_age=180,
    expose_headers=['X-Special-Header'],
    allow_credentials=True,
)


def setup_twitter_agent() -> twitter.Agent:
    return twitter.Agent(
        consumer_key=settings.TwitterConsumerKey,
        consumer_secret=settings.TwitterConsumerSecret,
        access_token=settings.TwitterAccessToken,
        access_token_secret=settings.TwitterAccessTokenSecret,
    )


def parse_tweets(
    app,
    tweets: list[twitter.TweetCopy],
) -> tuple[
    list[twitter.RunReport],
    list[twitter.ParseErrorTweet],
]:
    parsed = []
    errors = []

    app.log.info('starting to parse tweets...')
    for tweet in tweets:
        try:
            report = twitter.parse_tweet(tweet)
            parsed.append(report)

        except twitter.TweetParseError as e:
            app.log.error(e)
            app.log.error(tweet)
            etw = twitter.ParseErrorTweet(
                tweet=tweet,
                error_message=e.get_message(),
            )
            errors.append(etw)

    return parsed, errors


def render_date_contents(
    reports: list[twitter.RunReport],
    skip_target_date: date,
    force_save: bool = False,
    ignore_original: bool = False,
) -> None:
    outdir = f'{settings.ProcessorOutputDir}/date'
    recorder = recording.Recorder(
        partitioningRule=recording.PartitioningRuleByDate(),
        skipSaveRule=recording.SkipSaveRuleByDate(skip_target_date),
        fileStorage=storage.AmazonS3Storage(settings.S3Bucket),
        basedir=outdir,
        formats=(
            recording.OutputFormat.JSON,
            recording.OutputFormat.CSV,
            recording.OutputFormat.DATEHTML,
        ),
    )
    recorder.add_all(reports)

    if recorder.count():
        recorder.save(force=force_save, ignore_original=ignore_original)

    latestDatePageBuilder = recording.LatestDatePageBuilder(
        fileStorage=storage.AmazonS3Storage(settings.S3Bucket),
        basedir=outdir,
    )
    latestDatePageBuilder.build()


def render_user_contents(
    reports: list[twitter.RunReport],
    skip_target_date: date,
    force_save: bool = False,
    ignore_original: bool = False,
) -> None:
    outdir = f'{settings.ProcessorOutputDir}/user'
    recorder = recording.Recorder(
        partitioningRule=recording.PartitioningRuleByUser(),
        skipSaveRule=recording.SkipSaveRuleByDateAndUser(skip_target_date),
        fileStorage=storage.AmazonS3Storage(settings.S3Bucket),
        basedir=outdir,
        formats=(
            recording.OutputFormat.JSON,
            recording.OutputFormat.CSV,
            recording.OutputFormat.USERHTML,
        ),
    )
    recorder.add_all(reports)

    if recorder.count():
        recorder.save(force=force_save, ignore_original=ignore_original)

    recorder_byuserlist = recording.Recorder(
        partitioningRule=recording.PartitioningRuleByUserList(),
        skipSaveRule=recording.SkipSaveRuleNeverMatch(),
        fileStorage=storage.AmazonS3Storage(settings.S3Bucket),
        basedir=outdir,
        formats=(
            recording.OutputFormat.JSON,
            recording.OutputFormat.USERLISTHTML,
        )
    )
    recorder_byuserlist.add_all(reports)

    if recorder_byuserlist.count():
        recorder_byuserlist.save(force=force_save, ignore_original=ignore_original)


def render_quest_contents(
    reports: list[twitter.RunReport],
    skip_target_date: date,
    force_save: bool = False,
    ignore_original: bool = False,
) -> None:
    outdir = f'{settings.ProcessorOutputDir}/quest'
    recorder = recording.Recorder(
        partitioningRule=recording.PartitioningRuleByQuest(),
        skipSaveRule=recording.SkipSaveRuleByDateAndQuest(skip_target_date),
        fileStorage=storage.AmazonS3Storage(settings.S3Bucket),
        basedir=outdir,
        formats=(
            recording.OutputFormat.JSON,
            recording.OutputFormat.CSV,
            recording.OutputFormat.QUESTHTML,
        ),
    )
    recorder.add_all(reports)

    if recorder.count():
        recorder.save(force=force_save, ignore_original=ignore_original)

    recorder_byquestlist = recording.Recorder(
        # この partitioningRule は rebuild フラグを個別に渡す必要あり
        partitioningRule=recording.PartitioningRuleByQuestList(
            ignore_original,
        ),
        skipSaveRule=recording.SkipSaveRuleNeverMatch(),
        fileStorage=storage.AmazonS3Storage(settings.S3Bucket),
        basedir=outdir,
        formats=(
            recording.OutputFormat.JSON,
            recording.OutputFormat.QUESTLISTHTML,
        )
    )
    recorder_byquestlist.add_all(reports)
    # quest list だけはリストの増減がない場合でも数値の countup を
    # 再描画する必要があるので強制上書きが必要
    recorder_byquestlist.save(force=True, ignore_original=ignore_original)


def render_error_contents(
    errors: list[twitter.ParseErrorTweet],
    force_save: bool = False,
    ignore_original: bool = False,
) -> None:
    outdir = f'{settings.ProcessorOutputDir}/errors'
    recorder = recording.ErrorPageRecorder(
        fileStorage=storage.AmazonS3Storage(settings.S3Bucket),
        basedir=outdir,
        key='error',
        formats=(
            recording.ErrorOutputFormat.JSON,
            recording.ErrorOutputFormat.HTML,
        )
    )
    recorder.add_all(errors)
    recorder.save(force=force_save, ignore_original=ignore_original)


def render_contents(
    app,
    reports: list[twitter.RunReport],
    errors: list[twitter.ParseErrorTweet],
    skip_target_date: date,
    ignore_original: bool = False,
) -> None:
    render_date_contents(reports, skip_target_date, ignore_original=ignore_original)
    render_user_contents(reports, skip_target_date, ignore_original=ignore_original)
    render_quest_contents(reports, skip_target_date, ignore_original=ignore_original)

    render_error_contents(errors, ignore_original=ignore_original)
    app.log.info('done')


def render_month_contents(
    app,
    reports: list[twitter.RunReport],
    skip_target_date: date,
    force_save: bool = False,
):
    # month のレンダリングを render_contents() に含めないのは、日々の
    # collect tweets のアクションで追記していくには month のサイズが
    # 大きすぎるから。さすがに read/write のコストが無視できない。
    outdir = f'{settings.ProcessorOutputDir}/month'
    recorder = recording.Recorder(
        partitioningRule=recording.PartitioningRuleByMonth(),
        skipSaveRule=recording.SkipSaveRuleByDate(skip_target_date),
        fileStorage=storage.AmazonS3Storage(settings.S3Bucket),
        basedir=outdir,
        formats=(
            recording.OutputFormat.JSON,
            recording.OutputFormat.CSV,
            recording.OutputFormat.MONTHHTML,
        ),
    )
    recorder.add_all(reports)

    if recorder.count():
        # original への追記はせず、常に上書き
        recorder.save(force=force_save, ignore_original=True)

    # month の HTML レンダリングが完了してからでないと実行できない。
    # したがってこの位置で実行する。
    latestMonthPageBuilder = recording.LatestMonthPageBuilder(
        fileStorage=storage.AmazonS3Storage(settings.S3Bucket),
        basedir=outdir,
    )
    latestMonthPageBuilder.build()
    app.log.info('done')


@app.schedule(Rate(30, unit=Rate.MINUTES))
def collect_tweets(event):
    agent = setup_twitter_agent()

    tweet_repository = recording.TweetRepository(
        fileStorage=storage.AmazonS3Storage(settings.S3Bucket),
        basedir=settings.TweetStorageDir,
    )

    s3 = boto3.resource('s3')
    bucket = s3.Bucket(settings.S3Bucket)
    latest_tweet_id_file_key = \
        f'{settings.SettingsDir}/{settings.LatestTweetIDFile}'
    bio = io.BytesIO()
    app.log.info('checking latest_tweet_id file: %s', latest_tweet_id_file_key)
    try:
        bucket.download_fileobj(latest_tweet_id_file_key, bio)
        latest_tweet_id_str = bio.getvalue().decode('utf-8')
    except botocore.exceptions.ClientError as e:
        app.log.warning(e)
        latest_tweet_id_str = ''

    censored_accounts = twitter.CensoredAccounts(
        fileStorage=storage.AmazonS3Storage(settings.S3Bucket),
        filepath=f'{settings.SettingsDir}/{settings.CensoredAccountsFile}',
    )

    try:
        since_id = int(latest_tweet_id_str)
    except ValueError:
        since_id = None
    app.log.info(f'since_id: {since_id}')

    tweets = agent.collect(
        max_repeat=5,
        since_id=since_id,
        censored=censored_accounts,
    )
    app.log.info('collected %s tweets', len(tweets))

    if len(tweets) == 0:
        return

    tweet_log_file = '{}.json'.format(datetime.now().strftime('%Y%m%d_%H%M%S'))
    app.log.info('tweet_log: %s', tweet_log_file)
    tweet_repository.put(tweet_log_file, tweets)

    reports, errors = parse_tweets(app, tweets)
    # 定期収集において bydate の render を制限する必要はない
    skip_target_date = date(2000, 1, 1)
    render_date_contents(reports, skip_target_date)
    render_user_contents(reports, skip_target_date)
    render_quest_contents(reports, skip_target_date)

    render_error_contents(errors)

    latest_tweet = tweets[0]
    app.log.info('saving the latest tweet id: %s', latest_tweet.tweet_id)
    latest_tweet_id_bytes = str(latest_tweet.tweet_id).encode('utf-8')
    latest_tweet_id_stream = io.BytesIO(latest_tweet_id_bytes)
    bucket.upload_fileobj(latest_tweet_id_stream, latest_tweet_id_file_key)

    censored_accounts.save()


def split_tweets_by_date(
    tweets: Sequence[twitter.TweetCopy],
) -> dict[date, list[twitter.TweetCopy]]:
    """TweetCopyを投稿日で分類して返す。
    """
    d: dict[date, list[twitter.TweetCopy]] = {}

    for tw in tweets:
        # UTC 基準
        dt = tw.created_at.date()
        if dt in d:
            d[dt].append(tw)
        else:
            d[dt] = [tw]

    return d


@app.route("/recollect_tweets", methods=["POST"], cors=cors_config)
def recollect_tweets():
    # TODO implement rate limit
    request = app.current_request
    data = request.json_body
    app.log.info("json_body %s", data)

    if not isinstance(data, list):
        app.log.error("invalid data")
        raise BadRequestError("wrong format")

    if len(data) > 20:
        app.log.info("too many urls: %s", len(data))
        raise BadRequestError("wrong format")

    parser = twitter.StatusTweetURLParser()
    try:
        user_retargets_dict = parser.parse_multi(data)
    except twitter.TweetURLParseError:
        raise BadRequestError("wrong format")

    candidates: list[int] = []

    for user, targets in user_retargets_dict.items():
        repo = recording.UserOutputRepository(
            user,
            fileStorage=storage.AmazonS3Storage(settings.S3Bucket),
            basedir=f"{settings.ProcessorOutputDir}/user",
        )
        user_reports = repo.load()
        for target in targets:
            if target not in user_reports:
                candidates.append(target)
            else:
                app.log.info("requested tweet %s already exists", target)

    if not candidates:
        app.log.info("no candidates")
        return {"status": "ok"}

    app.log.info("candidates: %s", candidates)

    agent = setup_twitter_agent()
    tweet_dict = agent.get_multi(candidates)

    if not tweet_dict:
        app.log.info("no results")
        return {"status": "ok"}

    tweet_repository = recording.TweetRepository(
        fileStorage=storage.AmazonS3Storage(settings.S3Bucket),
        basedir=settings.TweetStorageDir,
    )

    tweets_by_date = split_tweets_by_date(tweet_dict.values())
    for dt, tweets in tweets_by_date.items():
        tweet_log_file = '{}.json'.format(dt.strftime('%Y%m%d_000000'))
        app.log.info('tweet_log: %s', tweet_log_file)
        tweet_repository.append_tweets(tweet_log_file, tweets)

        # TODO recollect した場合に month のレンダリングをどうするか未解決。
        # 問題になりそうなのは月をまたぐ場合。
        reports, errors = parse_tweets(app, tweets)
        # by date のレンダリングに制限は不要
        skip_target_date = date(2000, 1, 1)
        render_date_contents(reports, skip_target_date)
        render_user_contents(reports, skip_target_date)
        render_quest_contents(reports, skip_target_date)

        render_error_contents(errors)

    return {"status": "ok"}


@app.lambda_function()
def rebuild_outputs(event, context):
    skip_target_date_str = event.get("skipTargetDate")
    if skip_target_date_str:
        skip_target_date = date.fromisoformat(skip_target_date_str)
    else:
        skip_target_date = date(2000, 1, 1)

    skip_build_date = event.get("skipBuildDate", False)
    skip_build_user = event.get("skipBuildUser", False)
    skip_build_quest = event.get("skipBuildQuest", False)
    skip_build_month = event.get("skipBuildMonth", False)

    app.log.info("skip target date: %s", skip_target_date)

    tweet_repository = recording.TweetRepository(
        fileStorage=storage.AmazonS3Storage(settings.S3Bucket),
        basedir=settings.TweetStorageDir,
    )

    censored_accounts = twitter.CensoredAccounts(
        fileStorage=storage.AmazonS3Storage(settings.S3Bucket),
        filepath=f'{settings.SettingsDir}/{settings.CensoredAccountsFile}',
    )

    tweets = tweet_repository.readall(set(censored_accounts.list()))
    app.log.info('retrieved %s tweets', len(tweets))

    reports, errors = parse_tweets(app, tweets)

    if skip_build_date:
        app.log.info("skip building date contents")
    else:
        render_date_contents(reports, skip_target_date, ignore_original=True)

    if skip_build_user:
        app.log.info("skip building user contents")
    else:
        render_user_contents(reports, skip_target_date, ignore_original=True)

    if skip_build_quest:
        app.log.info("skip building quest contents")
    else:
        render_quest_contents(reports, skip_target_date, ignore_original=True)

    if skip_build_month:
        app.log.info("skip building month contents")
    else:
        render_month_contents(app, reports, skip_target_date)

    render_contents(app, reports, errors, skip_target_date, ignore_original=True)
    app.log.info('finished rebuilding outputs')


@app.lambda_function()
def build_static_contents(event, context):
    renderer = static.StaticPagesRenderer(
        fileStorage=storage.AmazonS3Storage(settings.S3Bucket),
        basedir=f'{settings.ProcessorOutputDir}/static',
    )
    renderer.render_all()
    app.log.info('finished building static contents')


def generate_caller_reference():
    # unique であればよい
    t = time.time()
    r = random.randint(0, 1048576)
    return f'harvest-{t}-{r}'


# @app.s3_event() を使うと複数のトリガーを設定できない。
# @app.lambda_function() で Lambda を定義して S3 トリガーは手動で設定する。
@app.lambda_function()
def invalidate_cloudfront_cache(event, context):
    logger.info(event)

    object_size = event['Records'][0]['s3']['object']['size']
    item = '/' + event['Records'][0]['s3']['object']['key']

    if not item.endswith('/index.html'):
        logger.info('ignore: %s', item)
        return

    logger.info("file size: %s", object_size)

    # ad hoc な方法ではあるが、現状これ以上良い解が見つかっていない。
    # blank html のサイズが 455, error page のサイズが 624 なので、その間の値にする。
    if object_size < 600:
        logger.info('ignore cache invalidation: probably blank html')
        return

    items = []
    # .../index.html を invalidate する代わりに
    # .../ を invalidate する。
    items.append(item[:item.rfind('/') + 1])

    logger.info('cache invalidation: %s', items)

    cloudfront.create_invalidation(
        DistributionId=settings.CloudfrontDistributionId,
        InvalidationBatch={
            'Paths': {
                'Quantity': len(items),
                'Items': items,
            },
            'CallerReference': generate_caller_reference(),
        }
    )
