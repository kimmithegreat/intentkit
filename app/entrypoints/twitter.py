import logging
import signal
import sys
import tweepy
from datetime import datetime, timedelta, timezone
from apscheduler.schedulers.blocking import BlockingScheduler
from sqlmodel import Session, select

from app.config.config import config
from app.core.ai import execute_agent
from app.models.agent import Agent, AgentPluginData, AgentQuota
from app.models.db import get_engine, init_db

logger = logging.getLogger(__name__)

# Set debug_resp to False
config.debug_resp = False


def create_twitter_client(config: dict) -> tweepy.Client:
    """Create a Twitter client from config.

    Args:
        config: Dictionary containing Twitter credentials

    Returns:
        tweepy.Client instance
    """
    return tweepy.Client(
        bearer_token=config.get("bearer_token"),
        consumer_key=config.get("consumer_key"),
        consumer_secret=config.get("consumer_secret"),
        access_token=config.get("access_token"),
        access_token_secret=config.get("access_token_secret"),
    )


def run_twitter_agents():
    """Get all agents from the database which twitter is enabled,
    check their twitter config, get mentions, and process them."""
    engine = get_engine()
    with Session(engine) as db:
        # Get all twitter-enabled agents
        agents = db.exec(
            select(Agent).where(
                Agent.twitter_enabled == True,  # noqa: E712
                Agent.twitter_config != None,  # noqa: E711
            )
        ).all()

        for agent in agents:
            try:
                # Get agent quota
                quota = AgentQuota.get(agent.id, db)

                # Check if agent has quota
                if not quota.has_twitter_quota(db):
                    logger.warning(
                        f"Agent {agent.id} has no twitter quota. "
                        f"Daily: {quota.twitter_count_daily}/{quota.twitter_limit_daily}, "
                        f"Total: {quota.twitter_count_total}/{quota.twitter_limit_total}"
                    )
                    continue

                # Initialize Twitter client
                if not agent.twitter_config:
                    logger.warning(f"Agent {agent.id} has no valid twitter config")
                    continue

                client = create_twitter_client(agent.twitter_config)
                me = client.get_me()
                if not me.data:
                    logger.error(
                        f"Failed to get Twitter user info for agent {agent.id}"
                    )
                    continue

                # Get last tweet id from plugin data
                plugin_data = AgentPluginData.get(agent.id, "twitter", "entrypoint", db)
                since_id = None
                if plugin_data and plugin_data.data:
                    since_id = plugin_data.data.get("last_tweet_id")
                # Always get mentions for the last day
                start_time = (
                    datetime.now(tz=timezone.utc) - timedelta(days=1)
                ).isoformat(timespec="milliseconds")
                # Get mentions
                mentions = client.get_users_mentions(
                    id=me.data.id,
                    max_results=10,
                    since_id=since_id,
                    start_time=start_time,
                    tweet_fields=["created_at", "author_id", "text"],
                )

                if not mentions.data:
                    logger.info(f"No new mentions for agent {agent.id}")
                    continue

                # Update last tweet id
                if mentions.meta:
                    last_tweet_id = mentions.meta.get("newest_id")
                    plugin_data = AgentPluginData(
                        agent_id=agent.id,
                        plugin="twitter",
                        key="entrypoint",
                        data={"last_tweet_id": last_tweet_id},
                    )
                    plugin_data.save(db)
                else:
                    raise Exception(f"Failed to get last tweet id for agent {agent.id}")

                # Process each mention
                for mention in mentions.data:
                    thread_id = f"{agent.id}-twitter-{mention.author_id}"
                    response = execute_agent(agent.id, mention.text, thread_id)

                    # Reply to the tweet
                    client.create_tweet(
                        text="\n".join(response), in_reply_to_tweet_id=mention.id
                    )

                # Update quota
                quota.add_twitter(db)

            except Exception as e:
                logger.error(
                    f"Error processing twitter mentions for agent {agent.id}: {str(e)}"
                )
                continue


if __name__ == "__main__":
    # Initialize infrastructure
    init_db(**config.db)

    # Create scheduler
    scheduler = BlockingScheduler()
    scheduler.add_job(run_twitter_agents, "interval", minutes=1)

    # Register signal handlers
    def signal_handler(signum, frame):
        scheduler.shutdown()
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        pass
