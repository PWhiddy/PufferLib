from pdb import set_trace as T
from collections import defaultdict

import torch
import copy

import numpy as np
import pandas as pd

from sqlalchemy import create_engine, Column, Integer, Boolean, String, Float, JSON, text, cast
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

from pufferlib.rating import OpenSkillRating


Base = declarative_base()

class Policy(Base):
    __tablename__ = 'policies'

    id = Column(Integer, primary_key=True)
    model_path = Column(String)
    model_class = Column(String)
    name = Column(String, unique=True)
    mu = Column(Float)
    sigma = Column(Float)
    episodes = Column(Integer)
    additional_data = Column(JSON)

    def __init__(self, *args, model=None, **kwargs):
        super(Policy, self).__init__(*args, **kwargs)
        if model:
            self.model = model

    def load_model(self, model):
        model.load_state_dict(torch.load(self.model_path))
        self.model = model.cuda()
 
    def save_model(self, model):
        torch.save(model.state_dict(), self.model_path)
        self.model_class = str(type(model))
        self.model = model


class PolicyDatabase:
    def __init__(self, path='sqlite:///policy_pool.db'):
        self.engine = create_engine(path, echo=False)
        Base.metadata.create_all(self.engine)
        Session = sessionmaker(bind=self.engine)
        self.session = Session()
        self.connection = self.engine.connect()
        self.connection.execute(text("PRAGMA journal_mode=WAL;"))

    def add_policy(self, policy):
        self.session.add(policy)
        self.session.commit()

    def query_policy_by_name(self, name):
        return self.session.query(Policy).filter_by(name=name).first()

    def query_tenured_policies(self):
        return self.session.query(Policy).filter(
            cast(Policy.additional_data['tenured'], Boolean) == True
        ).all()

    def query_untenured_policies(self):
        return self.session.query(Policy).filter(
            cast(Policy.additional_data['tenured'], Boolean) != True
        ).all()

    def delete_policy(self, policy):
        self.session.delete(policy)
        self.session.commit()

    def query_all_policies(self):
        return self.session.query(Policy).all()

    def update_policy(self, policy):
        self.session.commit()

class PolicyPool():
    def __init__(self, evaluation_batch_size, learner, name,
            sample_weights=[], active_policies=4,
            path='pool', mu=1000, anchor_mu=1000, sigma=100/3):

        assert len(sample_weights) == active_policies

        self.learner = learner
        self.learner_name = name
        self.allocated = False

        # Set up skill rating tournament
        self.tournament = OpenSkillRating(mu, anchor_mu, sigma)
        self.scores = defaultdict(list)
        self.mu = mu
        self.anchor_mu = anchor_mu
        self.sigma = sigma

        self.num_scores = 0
        self.num_active_policies = active_policies
        self.active_policies = []
        self.path = path
       
        # Set up the SQLite database and session
        self.database = PolicyDatabase()

        # Assign policies used for evaluation
        self.add_policy(learner, name, tenured=True, mu=mu, sigma=sigma, anchor=False)
        self.update_active_policies()

        # Create indices for splitting data across policies
        chunk_size = sum(sample_weights)
        assert evaluation_batch_size % chunk_size == 0
        pattern = [i for i, weight in enumerate(sample_weights)
                for _ in range(weight)]

        # Distribute indices among sublists
        self.sample_idxs = [[] for _ in range(len(sample_weights))]
        for idx in range(evaluation_batch_size):
            sublist_idx = pattern[idx % chunk_size]
            self.sample_idxs[sublist_idx].append(idx)

        # Learner mask
        self.learner_mask = np.zeros(evaluation_batch_size)
        self.learner_mask[self.sample_idxs[0]] = 1

    @property
    def ratings(self):
        return self.tournament.ratings

    def add_policy_copy(self, key, name, tenured=False, anchor=False):
        # Retrieve the policy from the database using the key
        original_policy = self.database.query_policy_by_name(key)
        assert original_policy is not None, f"Policy with name '{key}' does not exist."
        
        # Use add_policy method to add the new policy
        self.add_policy(original_policy.model, name, tenured=tenured, mu=original_policy.mu, sigma=original_policy.sigma, anchor=anchor)

    def add_policy(self, model, name, tenured=False, mu=None, sigma=None, anchor=False, overwrite_existing=True):
        # Construct the model path by joining the model and name
        model_path = f"{self.path}/{name}"
        
        # Check if a policy with the same name already exists in the database
        existing_policy = self.database.query_policy_by_name(name)

        if existing_policy is not None:
            if overwrite_existing:
                self.database.delete_policy(existing_policy)
            else:
                raise ValueError(f"A policy with the name '{name}' already exists.")

        # Set default values for mu and sigma if they are not provided
        if mu is None:
            mu = self.mu
        if sigma is None:
            sigma = self.sigma

        # TODO: Eliminate need to deep copy
        model = copy.deepcopy(model)
        policy = Policy(
            model=model,
            model_path=model_path,
            model_class=str(type(model)),
            name=name,
            mu=mu,
            sigma=sigma,
            episodes=0,  # assuming new policies have 0 episodes
            additional_data={'tenured': tenured}
        )
        policy.save_model(model)
        
        # Add the new policy to the database
        self.database.add_policy(policy)

        # Add the policy to the tournament system
        # TODO: Figure out anchoring
        if anchor:
            self.tournament.set_anchor(name)
        else:
            self.tournament.add_policy(name)
            self.tournament.ratings[name].mu = mu
            self.tournament.ratings[name].sigma = sigma

    def forwards(self, obs, lstm_state=None, dones=None):
        batch_size = len(obs)
        for samp, policy in zip(self.sample_idxs, self.active_policies):
            if lstm_state is not None:
                atn, lgprob, _, val, (lstm_state[0][:, samp], lstm_state[1][:, samp]) = policy.model.get_action_and_value(
                    obs[samp],
                    [lstm_state[0][:, samp], lstm_state[1][:, samp]],
                    dones[samp])
            else:
                atn, lgprob, _, val = policy.model.get_action_and_value(obs[samp])
            
            if not self.allocated:
                self.allocated = True

                self.actions = torch.zeros(batch_size, *atn.shape[1:], dtype=int).to(atn.device)
                self.logprobs = torch.zeros(batch_size).to(lgprob.device)
                self.values = torch.zeros(batch_size).to(val.device)

                if lstm_state is not None:
                    self.lstm_h = torch.zeros(batch_size, *lstm_state[0].shape[1:]).to(lstm_state[0].device)
                    self.lstm_c = torch.zeros(batch_size, *lstm_state[1].shape[1:]).to(lstm_state[1].device)

            self.actions[samp] = atn
            self.logprobs[samp] = lgprob
            self.values[samp] = val.flatten()

            if lstm_state is not None:
                self.lstm_h[samp] = lstm_state[0][:, samp]
                self.lstm_c[samp] = lstm_state[1][:, samp]

        if lstm_state is not None:
            return self.actions, self.logprobs, self.values, (self.lstm_h, self.lstm_c)
        return self.actions, self.logprobs, self.values, None

    def load(self, path):
        '''Load all models in path'''
        records = self.session.query(Policy).all()
        for record in records:
            model = eval(record.model_class)
            model.load_state_dict(torch.load(record.model_path))
            
            policy = Policy(model, record.name, record.model_path,
                                      record.mu, record.sigma, ...) # additional attributes

            self.policies[record.name] = policy

    def update_scores(self, infos, info_key):
        # TODO: Check that infos is dense and sorted
        agent_infos = []
        for info in infos:
            agent_infos += list(info.values())

        policy_infos = {}
        for samp, policy in zip(self.sample_idxs, self.active_policies):
            pol_infos = np.array(agent_infos)[samp]
            if policy.name not in policy_infos:
                policy_infos[policy.name] = list(pol_infos)
            else:
                policy_infos[policy.name] += list(pol_infos)

            for i in pol_infos:
                if info_key not in i:
                    continue 
            
                self.scores[policy.name].append(i[info_key])
                self.num_scores += 1

        return policy_infos

    def update_ranks(self):
        # Update the tournament rankings
        self.tournament.update(
            list(self.scores.keys()),
            list(self.scores.values())
        )
        
        # Update the mu and sigma values of each policy in the database
        for name, rating in self.tournament.ratings.items():
            policy = self.database.query_policy_by_name(name)
            if policy:
                policy.mu = rating.mu
                policy.sigma = rating.sigma
                self.database.update_policy(policy)
        
        # Reset the scores
        self.scores = defaultdict(list)

    def update_active_policies(self):
        learner_policy = self.database.query_policy_by_name(self.learner_name)
        all_policies = self.database.query_all_policies()

        self.active_policies = [learner_policy] + np.random.choice(all_policies, self.num_active_policies - 1, replace=True).tolist()

        for policy in self.active_policies:
            policy.load_model(copy.deepcopy(self.learner))

    def to_table(self):
        policies = self.session.query(Policy).all()

        data = []
        for policy in policies:
            model_name = policy.model_path.split('/')[-1]
            experiment = policy.model_path.split('/')[-2]
            checkpoint = int(model_name.split('.')[0])
            rank = self.tournament.ratings[policy.name].mu
            num_samples = policy.episodes
            data.append([model_name, rank, num_samples, experiment, checkpoint])

        table = pd.DataFrame(data, columns=["Model", "Rank", "Num Samples", "Experiment", "Checkpoint"]).sort_values(by='Rank', ascending=False)

        print(table[["Model", "Rank"]])
        